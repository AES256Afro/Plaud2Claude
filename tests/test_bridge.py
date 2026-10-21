"""Plaud2Claude's decisions and payloads, against a fake fire endpoint.
Run inside the image: docker run --rm -v "$PWD/tests:/tests" -w /app <image> python3 -m unittest discover -s /tests"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.update({"P2C_DATA": tempfile.mkdtemp(), "P2C_SYNC_EVERY": "0", "P2C_WAIT_FOR_SUMMARY": "30",
                   "CLAUDE_ROUTINE_URL": "trig_01TESTTESTTEST", "CLAUDE_ROUTINE_TOKEN": "sk-ant-oat01-test"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_routine as cr  # noqa: E402
import plaud2claude as p2c  # noqa: E402


class Fake(BaseHTTPRequestHandler):
    texts: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        Fake.texts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["text"])
        data = json.dumps({"type": "routine_fire", "claude_code_session_id": "session_01Y",
                           "claude_code_session_url": "https://claude.ai/code/session_01Y"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
cr.normalize_url = lambda value: f"http://127.0.0.1:{server.server_port}/fire"


def rec(rid="r1", minutes_ago=5):
    start = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"id": rid, "name": "1:1 with Sam", "duration": 1500000, "start_at": start.replace(tzinfo=None).isoformat()}


def fresh_state():
    s = {"since": (datetime.now() - timedelta(hours=1)).isoformat(), "sent": {}, "failed": {}, "first_seen": {}, "fires": []}
    p2c.save(s)
    return p2c.load()


NOTES = json.dumps([{"data_type": "auto_sum_note", "data_content": "## Summary\nPilot enrollment and training."},
                    {"data_type": "mark_memo", "data_content": ""}])


class Bridge(unittest.TestCase):
    def setUp(self):
        Fake.texts.clear()

    def test_reads_plauds_summary(self):
        self.assertEqual(p2c.plaud_notes(NOTES), [("Plaud summary", "## Summary\nPilot enrollment and training.")])
        self.assertEqual(p2c.plaud_notes("not json"), [])

    def test_waits_for_plauds_summary_then_sends_without_it(self):
        state = fresh_state()
        now = time.time()
        self.assertEqual(p2c.decide(rec(), state, "transcript", [], False, now), "waiting_summary")
        self.assertEqual(p2c.decide(rec(), state, "transcript", [], False, now + 31 * 60), "send")

    def test_skips_old_empty_sent_and_owner_errors(self):
        state = fresh_state()
        self.assertEqual(p2c.decide(rec(minutes_ago=600), state, "t", ["n"], False, time.time()), "before")
        self.assertEqual(p2c.decide(rec(), state, "  ", ["n"], False, time.time()), "no_transcript")
        state["sent"]["r1"] = {"session_url": "x"}
        self.assertEqual(p2c.decide(rec(), state, "t", ["n"], False, time.time()), "sent")
        self.assertEqual(p2c.decide(rec(), state, "t", ["n"], True, time.time()), "send")
        state["sent"].clear()
        state["failed"]["r1"] = {"error": "401", "retryable": False}
        self.assertEqual(p2c.decide(rec(), state, "t", ["n"], False, time.time()), "failed")
        state["failed"]["r1"]["retryable"] = True
        self.assertEqual(p2c.decide(rec(), state, "t", ["n"], False, time.time()), "send")

    def test_sends_summary_and_transcript(self):
        state = fresh_state()
        log: list = []
        self.assertTrue(p2c.send(rec(), "[00:00 - 00:04] Sam: hi", p2c.plaud_notes(NOTES), state, log.append))
        text = Fake.texts[0]
        self.assertIn("===== Plaud summary =====", text)
        self.assertIn("Pilot enrollment and training.", text)
        self.assertIn("Sam: hi", text)
        self.assertEqual(state["sent"]["r1"]["session_url"], "https://claude.ai/code/session_01Y")
        self.assertTrue(state["sent"]["r1"]["with_summary"])


if __name__ == "__main__":
    unittest.main()


class FakePlaud:
    """Just enough of the Plaud client for the bridge's loop."""

    def __init__(self, recs):
        self.recs = {r["id"]: r for r in recs if r.get("id")}

    def call_json(self, tool, file_id=None, **_):
        return self.recs[file_id]

    def call(self, tool, file_id=None, **_):
        if file_id == "broken":
            raise RuntimeError("Plaud error")
        return NOTES


class SweepOne(unittest.TestCase):
    """Regression tests for the first bug and security sweep: nothing is ever sent twice by accident."""

    def setUp(self):
        Fake.texts.clear()
        self.saved = (p2c.pa.unwrap_file, p2c.pa.fetch_transcript, p2c.pa.list_recordings, p2c.plaud.get)
        p2c.pa.unwrap_file = lambda x: x
        p2c.pa.fetch_transcript = lambda p, rid: "" if rid == "silent" else "[00:00 - 00:04] Sam: hi"

    def tearDown(self):
        p2c.pa.unwrap_file, p2c.pa.fetch_transcript, p2c.pa.list_recordings, p2c.plaud.get = self.saved

    def run_job(self, job, recs):
        fake = FakePlaud(recs)
        p2c.plaud.get = lambda: fake
        p2c.pa.list_recordings = lambda *a: list(recs)
        bridge = p2c.Bridge.__new__(p2c.Bridge)
        bridge.log, bridge.status, bridge.last_sync = [], {}, None
        bridge.say = bridge.log.append
        bridge._run(job)
        return bridge

    def test_an_attempt_is_saved_before_the_request_and_settled_after_a_crash(self):
        state = fresh_state()
        seen = []
        p2c.send(rec(), "t", [], state, lambda m: None, persist=lambda s: seen.append(dict(s["inflight"])))
        self.assertIn("r1", seen[0])
        self.assertEqual(state["inflight"], {})
        state["inflight"]["r2"] = {"at": "2026-10-20T10:00:00"}
        self.assertEqual(p2c.settle_inflight(state), 1)
        self.assertFalse(state["failed"]["r2"]["retryable"])
        self.assertEqual(p2c.decide(rec("r2"), state, "t", ["n"], False, time.time()), "failed")

    def test_a_damaged_state_is_kept_and_only_new_recordings_go(self):
        p2c.STATE.write_text("{half")
        state = p2c.load()
        self.assertTrue(state["damaged"])
        self.assertTrue(any(p.name.startswith("plaud2claude.json.damaged-") for p in p2c.STATE.parent.iterdir()))
        self.assertEqual(p2c.decide(rec(minutes_ago=60), state, "t", ["n"], False, time.time()), "before")

    def test_stale_send_clicks_dont_send_twice_but_send_again_does(self):
        fresh_state()
        self.run_job(("send", "r1", ""), [rec()])
        self.run_job(("send", "r1", ""), [rec()])  # an old page's "Send now", after the first went
        self.assertEqual(len(Fake.texts), 1)
        sent_at = p2c.load()["sent"]["r1"]["at"]
        self.run_job(("send", "r1", sent_at), [rec()])
        self.run_job(("send", "r1", sent_at), [rec()])  # the same "Send again" clicked twice
        self.assertEqual(len(Fake.texts), 2)

    def test_one_bad_recording_doesnt_stop_the_rest(self):
        fresh_state()
        bridge = self.run_job(("sync",), [{"name": "no id"}, rec("broken"), rec("silent"), rec("r3")])
        self.assertEqual(len(Fake.texts), 1)
        self.assertEqual(bridge.status["broken"], "error")
        self.assertIn("r3", p2c.load()["sent"])

    def test_retry_after_holds_every_send(self):
        state = fresh_state()
        state["not_before"] = time.time() + 600
        self.assertFalse(p2c.send(rec(), "t", [], state, lambda m: None))
        self.assertEqual(Fake.texts, [])
        self.assertTrue(state["failed"]["r1"]["retryable"])
