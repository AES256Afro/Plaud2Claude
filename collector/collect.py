"""Plaud2Claude collector for a PLAUD NotePin S or Note Pro.

Runs on the computer beside the recorder. It opens the device's encrypted Bluetooth session with the plaude-hla
toolkit (vendor/plaude-hla, a git submodule), lists and downloads recordings, decrypts each PLAUD.AI container
with the account's E2EE key, transcribes it on this machine with faster-whisper, and pushes the transcript to
Plaud2Claude's POST /api/recordings, which sends it to Claude. Plaud's cloud is used once, by provision.py.

Commands:
  probe      connect, handshake, list the recordings on the device (changes nothing)
  sync       download recordings not yet collected, decrypt, transcribe, push
  push ID    re-send an already transcribed recording to Plaud2Claude

Never sends delete, depair, format or firmware commands.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLKIT = HERE / "vendor" / "plaude-hla" / "toolkit"
IDENTITY = HERE / "identity.json"
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"
DEFAULT_OUT = Path.home() / "Plaud2Claude-collector"
DEFAULT_MODEL = "small"
DEFAULT_LANGUAGE = "en"
EPOCH_MIN, EPOCH_MAX = 1_500_000_000, 4_000_000_000  # session ids look like unix start times


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_identity() -> dict:
    ident = load_json(IDENTITY, None)
    if not ident:
        raise SystemExit(f"No {IDENTITY.name} yet. Run provision.py first (it asks for your Plaud sign-in).")
    # The toolkit reads its credentials from the environment at import time.
    os.environ["PLAUD_API_TOKEN"] = ident["api_token"]
    os.environ["PLAUD_DEVICE_SN"] = ident["device_sn"]
    os.environ["PLAUD_SN_SIGNATURE_B64"] = ident["sn_signature_b64"]
    os.environ["PLAUD_RSA_PRIVATE_KEY_PEM"] = ident["transport_private_key_pem"]
    os.environ["PLAUD_RSA_PUBLIC_KEY_PEM"] = ident["transport_public_key_pem"]
    return ident


# ---------------------------------------------------------------- the toolkit

def normalize_token(bind_token: str) -> str:
    """The handshake token as the official SDK sends it: the account token's `sub` claim without a `client_user_`
    prefix and without dashes (plaudio's observation). Unchanged when neither is present."""
    token = bind_token[len("client_user_"):] if bind_token.startswith("client_user_") else bind_token
    return token.replace("-", "")


def patched_handshake(original):
    """The toolkit's build_handshake, with the token normalized first."""

    def build_handshake(bind_token: str = "", sub_type: int = 0, port_version: int = 0) -> bytes:
        return original(normalize_token(bind_token), sub_type, port_version)

    return build_handshake


def toolkit():
    """Import the plaude-hla toolkit with its SDK-app login replaced by the stored account token."""
    if not (TOOLKIT / "plaud" / "cli.py").is_file():
        raise SystemExit(f"The plaude-hla toolkit is missing under {TOOLKIT}. Run: git submodule update --init")
    sys.path.insert(0, str(TOOLKIT))
    Path("/tmp").mkdir(exist_ok=True)  # the toolkit writes the session's RSA response there (C:\tmp on Windows)
    import plaud.cli as cli
    cli.full_sdk_auth = lambda: os.environ["PLAUD_API_TOKEN"]
    cli.build_handshake = patched_handshake(cli.build_handshake)
    return cli


# ---------------------------------------------------------------- names and times

def when(session_id: int) -> datetime | None:
    """When a recording started, in local time with its offset: the device's session id is its unix start time."""
    return datetime.fromtimestamp(session_id).astimezone() if EPOCH_MIN < session_id < EPOCH_MAX else None


def rec_id(device_sn: str, session_id: int) -> str:
    """The id Plaud2Claude sees: the serial's last four characters and the session id, like 0001-1760000000."""
    return f"{device_sn[-4:]}-{session_id}"


def stem_for(session_id: int) -> str:
    stamp = when(session_id)
    return f"notepin_{stamp:%Y%m%d_%H%M%S}" if stamp else f"notepin_{session_id}"


def describe(f) -> str:
    stamp = when(f.session_id)
    shown = stamp.strftime("%Y-%m-%d %H:%M") if stamp else "?"
    return f"session {f.session_id}  {shown}  {f.file_size / 1024:.0f} KiB  attr={f.attribute} scene={f.scene}"


# ---------------------------------------------------------------- device session

async def connect_and_list(cli, address: str | None, scan_seconds: float):
    ok = await cli._do_sdk_connect(address, scan_seconds)
    if not ok:
        raise SystemExit("Could not open a session with the device (see the toolkit output above).")
    return await cli._file_sync.get_file_list()


async def disconnect(cli) -> None:
    if cli.transport.is_connected:
        await cli.transport.disconnect()
    cli._crypto = None
    cli._file_sync = None


async def cmd_probe(args) -> None:
    ident = load_identity()
    cli = toolkit()
    try:
        files = await connect_and_list(cli, args.address, args.scan_seconds)
        tail = ident["device_sn"][-4:]
        print(f"\n{ident['device_name']} (serial ...{tail}): {len(files.files)} recording(s), {files.total} reported")
        for f in files.files:
            print("  " + describe(f))
    finally:
        await disconnect(cli)


# ---------------------------------------------------------------- decrypt, transcribe, push

def decrypt(ident: dict, raw: Path, ogg: Path) -> None:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from plaud import plaud_decrypt
    key = load_pem_private_key(ident["e2ee_private_key_pem"].encode(), password=None)
    plaud_decrypt.decrypt_plaud_file(str(raw), key, str(ogg))


def load_model(cfg: dict):
    """faster-whisper on the CPU, loaded once per run. The first use downloads the model."""
    from faster_whisper import WhisperModel
    name = cfg.get("whisper_model") or DEFAULT_MODEL
    print(f"  loading Whisper model {name} (the first run downloads it)")
    return WhisperModel(name, device="cpu", compute_type="int8"), name


def transcribe(model, audio: Path, model_name: str, language: str | None) -> dict:
    """Transcribe one OGG/Opus file (faster-whisper decodes it with PyAV; no WAV step) into the transcript JSON
    the push expects: {file, duration_seconds, model, language, segments: [{start, end, text}], text}."""
    segments, info = model.transcribe(str(audio), language=language or None, beam_size=5)
    segs = [{"start": round(float(s.start), 2), "end": round(float(s.end), 2), "text": s.text.strip()}
            for s in segments if s.text and s.text.strip()]
    return {"file": audio.stem, "duration_seconds": round(float(info.duration), 2), "model": model_name,
            "language": info.language, "segments": segs, "text": " ".join(s["text"] for s in segs)}


def build_push(cfg: dict, rid: str, name: str, recorded_at: str, transcript: dict, again: bool = False) -> dict:
    """The body for POST /api/recordings, from a transcript JSON (the shape transcribe() writes, which is also
    OpenPlaudit's): `text` becomes `transcript`; `segments` and `duration_seconds` go as they are."""
    body = {"id": rid, "name": name, "transcript": transcript.get("text") or "", "again": bool(again),
            "source": cfg.get("source") or f"collector on {socket.gethostname()}",
            "model": f"whisper {transcript.get('model') or '?'}"}
    if recorded_at:
        body["recorded_at"] = recorded_at
    duration = transcript.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        body["duration_seconds"] = duration
    segs = [{"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"])}
            for s in transcript.get("segments") or [] if isinstance(s, dict) and {"start", "end", "text"} <= set(s)]
    if segs:
        body["segments"] = segs
    return body


def push(cfg: dict, body: dict) -> dict:
    """POST the body to Plaud2Claude. Without server_url and ingest_token the transcript only stays local."""
    url, token = (cfg.get("server_url") or "").rstrip("/"), cfg.get("ingest_token") or ""
    if not url or not token:
        print("  (no server_url / ingest_token in config.json: transcript kept locally, not pushed)")
        return {"pushed": False, "reason": "not configured"}
    if not body["transcript"].strip():
        print("  (no speech found: nothing to push)")
        return {"pushed": False, "reason": "no speech"}
    import requests
    r = requests.post(f"{url}/api/recordings", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    try:
        reply = r.json()
    except ValueError:
        reply = {"text": r.text[:200]}
    print(f"  Plaud2Claude: HTTP {r.status_code} {reply}")
    return {"pushed": r.status_code in (200, 202), "http": r.status_code, "reply": reply,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# ---------------------------------------------------------------- commands

async def cmd_sync(args) -> None:
    ident = load_identity()
    cfg = load_json(CONFIG, {})
    state = load_json(STATE, {"done": {}})
    out = Path(cfg.get("output_dir") or DEFAULT_OUT).expanduser()
    for sub in ("raw", "audio", "transcripts"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    cli = toolkit()
    new = []
    try:
        files = await connect_and_list(cli, args.address, args.scan_seconds)
        todo = [f for f in files.files if str(f.session_id) not in state["done"]]
        if args.limit:
            todo = todo[: args.limit]
        print(f"\n{len(files.files)} recording(s) on the device, {len(todo)} to collect")
        for f in todo:
            raw = out / "raw" / f"{ident['device_sn']}-{f.session_id}.plaud"
            print("  downloading " + describe(f))
            await cli._file_sync.download_file(f.session_id, f.file_size, output_path=str(raw))
            new.append((f, raw))
    finally:
        await disconnect(cli)

    model = model_name = None
    done = 0
    for f, raw in new:
        stem, stamp = stem_for(f.session_id), when(f.session_id)
        ogg = out / "audio" / f"{stem}.ogg"
        entry = {"session_id": f.session_id, "raw": str(raw), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            decrypt(ident, raw, ogg)
            entry["audio"] = str(ogg)
            if model is None:
                model, model_name = load_model(cfg)
            print(f"  {stem}: decrypted, transcribing with faster-whisper ({model_name})")
            transcript = transcribe(model, ogg, model_name, cfg.get("language", DEFAULT_LANGUAGE))
            tpath = out / "transcripts" / f"{stem}.json"
            save_json(tpath, transcript)
            entry["transcript"] = str(tpath)
            body = build_push(cfg, rec_id(ident["device_sn"], f.session_id), stem,
                              stamp.isoformat(timespec="seconds") if stamp else "", transcript)
            entry["push"] = push(cfg, body)
            print(f"  {stem}: {transcript['duration_seconds']}s, {transcript['text'][:100]!r}")
        except Exception as err:
            entry["error"] = f"{type(err).__name__}: {err}"
            print(f"  {stem}: FAILED {entry['error']} (kept {raw.name}; it will be retried next sync)")
            continue
        state["done"][str(f.session_id)] = entry
        save_json(STATE, state)
        done += 1
    print(f"\nDone: {done} of {len(new)} new recording(s) processed")


def cmd_push(args) -> None:
    ident = load_identity()
    cfg = load_json(CONFIG, {})
    state = load_json(STATE, {"done": {}})
    entry = state["done"].get(args.session_id)
    if not entry or not entry.get("transcript"):
        raise SystemExit(f"No transcript for session {args.session_id} in {STATE.name}")
    transcript = json.loads(Path(entry["transcript"]).read_text(encoding="utf-8"))
    sid = int(args.session_id)
    stamp = when(sid)
    body = build_push(cfg, rec_id(ident["device_sn"], sid), Path(entry["transcript"]).stem,
                      stamp.isoformat(timespec="seconds") if stamp else "", transcript, again=args.again)
    entry["push"] = push(cfg, body)
    save_json(STATE, state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("probe", "sync"):
        p = sub.add_parser(name, help=__doc__.split(name, 1)[1].split("\n")[0].strip())
        p.add_argument("--address", help="Bluetooth address (default: scan by name; the pin rotates its address)")
        p.add_argument("--scan-seconds", type=float, default=12)
        if name == "sync":
            p.add_argument("--limit", type=int, default=0, help="at most this many new recordings this run")
    p = sub.add_parser("push", help="re-send an already transcribed recording to Plaud2Claude")
    p.add_argument("session_id")
    p.add_argument("--again", action="store_true", help="send even if Plaud2Claude already has it")
    args = ap.parse_args()
    if args.cmd == "probe":
        asyncio.run(cmd_probe(args))
    elif args.cmd == "sync":
        asyncio.run(cmd_sync(args))
    else:
        cmd_push(args)


if __name__ == "__main__":
    main()
