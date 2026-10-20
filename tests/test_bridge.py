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
