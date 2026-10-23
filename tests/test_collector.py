"""The collector's pure parts: the serial parser, the handshake-token fix, the push body against the server's
contract, and the transcript JSON shape. No Bluetooth, no model, no toolkit import; runs like the other tests
(inside the image with `collector/` mounted at /collector, or locally with OpenPlaud's modules on PYTHONPATH)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_bridge  # noqa: F401  sets the environment before plaud2claude is imported

import plaud2claude as p2c  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
import collect  # noqa: E402
import provision  # noqa: E402

# Synthetic advertisements (Realtek company id already stripped, as bleak does). No real serial in here.
NOTEPIN_S = bytes.fromhex("04" "56070201" "08" "8820000000000001" "44" "1400" "04" "f7231861" "0101")
NOTE_PRO = bytes.fromhex("02" "7103" "04" "56000701" "08" "8810000000000001" "44" "1400" "040100")

TRANSCRIPT = {"file": "notepin_20261022_140300", "duration_seconds": 61.5, "model": "small", "language": "en",
              "segments": [{"start": 0.0, "end": 2.5, "text": "Line one."}, {"start": 2.5, "end": 5.0, "text": "Line two."}],
              "text": "Line one. Line two."}


class Serial(unittest.TestCase):
    def test_notepin_s_layout(self):
        self.assertEqual(provision.serial_from_manufacturer(NOTEPIN_S), "8820000000000001")

    def test_note_pro_layout_with_project_id(self):
        self.assertEqual(provision.serial_from_manufacturer(NOTE_PRO), "8810000000000001")

    def test_anything_else_is_ignored(self):
        self.assertIsNone(provision.serial_from_manufacturer(NOTEPIN_S[:10]))
        self.assertIsNone(provision.serial_from_manufacturer(b"\x03" + NOTEPIN_S[1:]))
        self.assertIsNone(provision.serial_from_manufacturer(b""))


class Handshake(unittest.TestCase):
    def test_the_token_loses_its_prefix_and_dashes(self):
        self.assertEqual(collect.normalize_token("client_user_1234-5678-abcd"), "12345678abcd")
        self.assertEqual(collect.normalize_token("12345678abcd"), "12345678abcd")
        self.assertEqual(collect.normalize_token(""), "")

    def test_both_spellings_give_the_same_packet(self):
        calls = []

        def original(bind_token="", sub_type=0, port_version=0):
            calls.append((bind_token, sub_type, port_version))
            return f"{bind_token}|{sub_type}|{port_version}".encode()

        build = collect.patched_handshake(original)
        self.assertEqual(build("client_user_1234-5678-abcd", 0, 20), build("12345678abcd", 0, 20))
        self.assertEqual(calls, [("12345678abcd", 0, 20)] * 2)


class Push(unittest.TestCase):
    def test_the_body_meets_the_servers_contract(self):
        cfg = {"source": "notepin-s via collector on test-pc"}
        body = collect.build_push(cfg, "0001-1760000000", "notepin_20261022_140300", "2026-10-22T14:03:00-04:00", TRANSCRIPT)
        self.assertIsNone(p2c.check_local(body))
        self.assertEqual(body["id"], "0001-1760000000")
        self.assertEqual(body["transcript"], "Line one. Line two.")
        self.assertEqual(body["segments"], TRANSCRIPT["segments"])
        self.assertEqual((body["duration_seconds"], body["model"], body["again"]), (61.5, "whisper small", False))
        self.assertEqual(body["recorded_at"], "2026-10-22T14:03:00-04:00")
        rec = p2c.local_rec(body)
        self.assertEqual((rec["duration"], rec["name"]), (61500, "notepin_20261022_140300"))
        self.assertEqual(p2c.local_transcript(body), "[00:00 - 00:02] Line one.\n[00:02 - 00:05] Line two.")
        self.assertIn("Transcribed by notepin-s via collector on test-pc with whisper small.", p2c.local_notes(body)[0][1])

    def test_optional_fields_are_left_out_when_unknown(self):
        body = collect.build_push({}, "0001-7", "notepin_7", "", {"text": "words", "model": "small"})
        self.assertIsNone(p2c.check_local(body))
        self.assertNotIn("recorded_at", body)
        self.assertNotIn("duration_seconds", body)
        self.assertNotIn("segments", body)
        self.assertTrue(body["source"].startswith("collector on "))
        again = collect.build_push({}, "0001-7", "notepin_7", "", {"text": "words"}, again=True)
        self.assertEqual((again["again"], again["model"]), (True, "whisper ?"))

    def test_ids_and_times_come_from_the_device(self):
        self.assertEqual(collect.rec_id("8820000000000001", 1760000000), "0001-1760000000")
        self.assertEqual(collect.when(1760000000).timestamp(), 1760000000)
        self.assertIsNotNone(collect.when(1760000000).utcoffset())  # carries the local offset for recorded_at
        self.assertIsNone(collect.when(42))
        self.assertEqual(collect.stem_for(42), "notepin_42")
        self.assertTrue(collect.stem_for(1760000000).startswith("notepin_2025"))


class Transcribe(unittest.TestCase):
    class StubModel:
        """Just faster-whisper's transcribe() shape: a segment iterator and an info object."""

        def __init__(self):
            self.calls = []

        def transcribe(self, path, **kw):
            self.calls.append((path, kw))
            segs = iter([SimpleNamespace(start=0.0, end=2.5, text=" Line one."), SimpleNamespace(start=2.5, end=5.004, text=" Line two. "),
                         SimpleNamespace(start=5.0, end=6.0, text="   ")])
            return segs, SimpleNamespace(duration=61.5, language="en", language_probability=0.99)

    def test_the_transcript_json_has_the_expected_shape(self):
        model = self.StubModel()
        out = collect.transcribe(model, Path("C:/x/notepin_20261022_140300.ogg"), "small", "en")
        self.assertEqual(out, TRANSCRIPT)
        self.assertEqual(model.calls[0][1], {"language": "en", "beam_size": 5})
        self.assertTrue(model.calls[0][0].endswith("notepin_20261022_140300.ogg"))

    def test_no_language_means_detect(self):
        model = self.StubModel()
        collect.transcribe(model, Path("a.ogg"), "small", "")
        self.assertIsNone(model.calls[0][1]["language"])

    def test_push_skips_silence_and_missing_config(self):
        self.assertEqual(collect.push({}, {"transcript": "words"})["reason"], "not configured")
        cfg = {"server_url": "https://box.example", "ingest_token": "t" * 24}
        self.assertEqual(collect.push(cfg, {"transcript": "   "})["reason"], "no speech")


if __name__ == "__main__":
    unittest.main()
