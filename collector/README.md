# Plaud2Claude collector

A small Python tool for the computer that sits beside a **PLAUD NotePin S** or **Note Pro**. It pulls recordings
off the device over Bluetooth LE, decrypts them with your own account's key, transcribes them on that computer
with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and pushes each transcript to Plaud2Claude's
`POST /api/recordings` (see the main README's *Local recordings*), which opens a Claude session for it. Plaud's
cloud is used once, during provisioning; after that nothing leaves the computer except the push to your own server.

The Bluetooth part is the [plaude-hla](https://github.com/hardware-liberation-army/plaude-hla) toolkit, vendored
as a git submodule under `vendor/plaude-hla` and driven unmodified (see *License* at the end).

## Which devices

| Device | Chip family | Advertisement | Session |
| --- | --- | --- | --- |
| PLAUD NotePin S | Realtek (company id 0x005D) | one Realtek manufacturer section `04 <4 bytes> 08 <8-byte serial> 44 1400 …`, no project-id field | encrypted, protocol version 20 |
| PLAUD Note Pro | Realtek | `02 <project id u16 = 881> 04 <4 bytes> 08 <8-byte serial> …` | encrypted, protocol version 20 |
| PLAUD Note (original) | Nordic, plain BLE | | not this collector: use [OpenPlaudit](https://github.com/mar-maradnus/openplaudit) |

Both Realtek devices expose the PLAUD service 0x1910 with the same write/notify characteristics. The NotePin S
rotates its Bluetooth address every few minutes, so the tools find it by name and read the serial from the
manufacturer data, not from the address.

## What you need

- Windows, macOS or Linux with Bluetooth LE, and Python 3.11 or newer (checked with 3.14).
- The Plaud phone app disconnected while the collector runs: the device holds one connection.
- The Plaud account the device is registered to. The device only opens a session for a host that presents a
  signature over its serial issued by Plaud, so one trip to Plaud's API is unavoidable.
- Plaud2Claude 0.4.0 or newer with `P2C_INGEST_TOKEN` set (BoxPilot generates it).

## Install

```bash
git clone --recurse-submodules https://github.com/AES256Afro/Plaud2Claude
cd Plaud2Claude/collector
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # macOS, Linux
.venv\Scripts\pip install -r requirements.txt    # Windows
```

If the clone was made without `--recurse-submodules`, run `git submodule update --init` once. On Windows,
`collect.cmd` runs every command below with that `.venv` (`collect.cmd provision …`, `collect.cmd probe`,
`collect.cmd sync`); on macOS and Linux use `.venv/bin/python collect.py …`. The first `sync` downloads the
Whisper model (`small`, about 500 MB) into Hugging Face's cache.

## One-time provisioning (you run this yourself)

```bash
python provision.py
```

It asks for your Plaud e-mail and password in the terminal (not echoed, sent only to Plaud's login endpoint), or
takes a token copied from web.plaud.ai with `--token <token>` instead. Then it:

1. checks the token (`/user/me`; `--region eu` to try Plaud's EU endpoint first),
2. scans Bluetooth for the device's serial (press the device's button to wake it; `--serial <16 hex>` skips the scan)
   and checks it against your account's device list,
3. generates a local RSA-2048 key pair for the Bluetooth session key exchange,
4. fetches the serial signature from `POST /device/sn-sign`,
5. fetches the account's E2EE private key from `POST /e2ee/gen_key`,

and writes `identity.json` next to the scripts, readable only by your user. It holds the account token, the
serial, the signature, the account decryption key and the session key pair, so treat it like a password. It is
gitignored, as are `config.json`, `state.json` and `.venv/`. Running provisioning again backs the old file up as
`identity.json.bak.<time>`.

Then, with the phone app closed:

```bash
python collect.py probe
```

This connects, completes the encrypted handshake, lists the recordings on the device, and changes nothing. If
the device refuses the handshake, provision again with `--sign-with-key`: the signature then also covers the
new Bluetooth public key, which is how the official SDK asks for it.

## Daily use

Copy `config.example.json` to `config.json` and fill it in:

| Key | What it is | Default |
| --- | --- | --- |
| `server_url` | Plaud2Claude's address on your tailnet, like `https://<your-box>.ts.net:8796` | (none: transcripts stay local) |
| `ingest_token` | its `P2C_INGEST_TOKEN` | (none) |
| `source` | shown on Plaud2Claude's page and told to Claude | `collector on <host name>` |
| `output_dir` | where recordings and transcripts are kept | `~/Plaud2Claude-collector` |
| `whisper_model` | any faster-whisper model name (`tiny`, `base`, `small`, `medium`, `large-v3`…) | `small` |
| `language` | the recordings' language code, or empty to detect | `en` |

Then, after a meeting or on a schedule (Task Scheduler, cron, launchd):

```bash
python collect.py sync
```

Each run is one Bluetooth session. It downloads every recording not yet in `state.json` into
`<output_dir>/raw/<serial>-<session>.plaud`, decrypts it to `<output_dir>/audio/notepin_<date>_<time>.ogg`,
transcribes that OGG directly (no WAV step; the OGG is kept as the audio copy) into
`<output_dir>/transcripts/<same stem>.json`, and pushes it. A recording that fails at any step is retried on
the next run. `--limit N` takes at most N new recordings; `--address` and `--scan-seconds` tune the scan.

Whisper runs on the CPU (`int8`), so a one-hour recording takes several minutes with `small`. Without a GPU,
`small` is the sensible choice; `medium` is more accurate and several times slower.

`python collect.py push <session id>` sends an already transcribed recording again; add `--again` when
Plaud2Claude says it is already in Claude and you want a second session anyway.

### What Plaud2Claude receives

The push follows the contract in the main README: `id` is the serial's last four characters and the session id
(`0001-1760000000`), `name` is the file stem, `recorded_at` comes from the session id (the device's unix start
time, with your local offset), `duration_seconds` and `segments` from faster-whisper, `source` from the config and
`model` as `whisper <name>`. With segments present, Claude reads timestamped lines, like a Plaud transcript.
The transcript JSON on disk has the shape `{file, duration_seconds, model, language, segments, text}`, the same as
OpenPlaudit writes, so a transcript made some other way can be pushed with the same mapping.

## Safety

- The collector never sends delete, depair, format or firmware commands. It uses the toolkit's connect sequence,
  file list and download only; recordings stay on the device until you delete them from the phone app.
- The toolkit prints session key material to the console (the RSA response, the derived key and nonce) and
  writes a copy to `/tmp/plaud_rsa_response.*` (`C:\tmp` on Windows). That is local only; delete it if you like.
- `identity.json` is a credential. Keep it with the collector and out of backups you share.

## Status

As of 2026-10-22 this is **smoke-tested without credentials**: argument parsing, importing and patching the
toolkit, the serial parser on both advertisement layouts, the handshake token fix, the transcript shape, and the
push body against Plaud2Claude's own validation. Not yet verified on a real device:

- the encrypted handshake on a NotePin S with an ordinary account token (the toolkit proved it on a Note Pro
  with an SDK application token; the collector derives the handshake token the way the official SDK does);
- decrypting a NotePin S recording with the account's E2EE key;
- the push, end to end, from this collector.

A fresh install today gets bleak 3.x; the toolkit was written against bleak 0.2x. And every protocol here is
reverse-engineered: a PLAUD firmware update can break any step.

## License

`vendor/plaude-hla` is the Hardware Liberation Army's plaude-hla toolkit, included unmodified as a submodule
pinned to commit `6861371`, under the **Hardware Liberation License** (see `vendor/plaude-hla/LICENSE`). That
license requires attribution: keep its copyright notice, conditions and disclaimer with any copy, and mark
modified versions as modified. The collector does not modify the toolkit's files; it replaces two of its functions
at run time (the SDK-app login, with the stored account token, and the handshake builder, with the token fix),
and says so here. The toolkit's `third_party/` holds material under its own licenses that the collector does not
use.
