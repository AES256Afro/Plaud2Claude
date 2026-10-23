"""Local recordings: a collector's push to POST /api/recordings, and the job that sends it to Claude without Plaud.
Runs like test_bridge.py (inside the image, or with OpenPlaud's modules on PYTHONPATH)."""

import http.client
import json
import queue
import shutil
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import test_bridge  # noqa: F401  sets the environment and the fake fire endpoint before plaud2claude is imported
from test_bridge import Fake, fresh_state, rec

import plaud2claude as p2c  # noqa: E402

TOKEN = "i" * 24
PAGE_STATE = {"state": "signed_out", "who": None, "url": None, "message": None, "failed": False}


def push(rid="loc1", **extra):
    """What a collector sends: OpenPlaudit's transcript JSON mapped onto the contract."""
    body = {"id": rid, "name": "Standup", "transcript": "Line one.\nLine two.", "recorded_at": "2026-10-22T14:03:00-04:00",
            "duration_seconds": 61.5, "source": "plaude on chris-pc", "model": "whisper small",
            "segments": [{"start": 0.0, "end": 2.5, "text": "Line one."}, {"start": 2.5, "end": 5.0, "text": "Line two."}]}
    body.update(extra)
    return {k: v for k, v in body.items() if v is not ...}


def stub_bridge():
    """Queues jobs like the real one, with no worker thread, so a test can look at what was queued."""
    b = p2c.Bridge.__new__(p2c.Bridge)
    b.queue, b.pending, b.adding = queue.Queue(), set(), threading.Lock()
    b.log, b.status, b.last_sync, b.current = [], {}, None, None
    b.clock = p2c.core.CheckClock(0)
    b.say = b.log.append
    return b


server = ThreadingHTTPServer(("127.0.0.1", 0), p2c.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def post(path, body=None, token=TOKEN, raw=None, headers=None, ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    data = raw if raw is not None else json.dumps(body).encode()
    h = {"Content-Type": ctype}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    conn.request("POST", path, data, h)
    r = conn.getresponse()
    text = r.read().decode()
    conn.close()
    try:
        return r.status, json.loads(text)
    except ValueError:
        return r.status, text


def boom():
    raise AssertionError("a local recording must never touch the Plaud session")


class LocalCase(unittest.TestCase):
    def setUp(self):
        Fake.texts.clear()
        fresh_state()
        shutil.rmtree(p2c.LOCAL, ignore_errors=True)
        p2c._local_cache.clear()
        p2c.INGEST_TOKEN = TOKEN
        p2c.bridge = stub_bridge()
        self.saved = (p2c.plaud.get, p2c.signin.page_state, p2c.passwords)
        p2c.plaud.get = boom
        p2c.signin.page_state = lambda: dict(PAGE_STATE)

    def tearDown(self):
        p2c.plaud.get, p2c.signin.page_state, p2c.passwords = self.saved
        p2c.INGEST_TOKEN = ""

    def queued(self):
        """The jobs queued so far, taken off the queue as the worker would."""
        jobs = []
        while not p2c.bridge.queue.empty():
            jobs.append(p2c.bridge.queue.get_nowait())
            p2c.bridge.pending.discard(jobs[-1])
        return jobs


class Ingest(LocalCase):
    def test_the_endpoint_is_off_without_a_token(self):
        p2c.INGEST_TOKEN = ""
        status, _ = post("/api/recordings", push())
        self.assertEqual(status, 404)
        self.assertEqual(self.queued(), [])

    def test_it_needs_the_ingest_token(self):
        for token in (None, "", "nope", TOKEN[:-1], "t" * 24):
            status, body = post("/api/recordings", push(), token=token)
            self.assertEqual((status, body["error"]), (403, "wrong or missing ingest token"), token)
        self.assertEqual(self.queued(), [])
        self.assertFalse(p2c.local_file("loc1").exists())

    def test_no_cookie_or_page_password_is_needed(self):
        p2c.passwords = p2c.core.Passwords("a page password", b"plaud2claude-session:", 30)
        status, body = post("/api/recordings", push())
        self.assertEqual((status, body["queued"]), (202, True))
        status, _ = post("/send", raw=b"id=loc1", token=None, ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 401)  # the page's own form still wants the cookie

    def test_bad_json_is_refused(self):
        for raw in (b"{half", b"[]", b'"text"', b"null"):
            status, body = post("/api/recordings", raw=raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("JSON", body["error"])
        status, body = post("/api/recordings", raw=b"", headers={"Content-Length": "0"})
        self.assertEqual(status, 400)
        self.assertEqual(self.queued(), [])

    def test_bad_fields_are_refused(self):
        bad = [push(id="a/b"), push(id=""), push(id="x" * 81), push(id=7), push(transcript=""), push(transcript="   "),
               push(transcript=...), push(transcript=["no"]), push(name=3), push(recorded_at="yesterday"),
               push(duration_seconds="long"), push(duration_seconds=-1), push(duration_seconds=True),
               push(segments=[{"start": "0", "end": 1, "text": "x"}]), push(segments=[{"start": 0, "end": 1}]),
               push(segments="none"), push(again="yes"), push(source=["pc"])]
        for body in bad:
            status, answer = post("/api/recordings", body)
            self.assertEqual(status, 400, body)
            self.assertTrue(answer["error"], body)
        self.assertEqual(self.queued(), [])
        self.assertFalse(p2c.LOCAL.exists())

    def test_too_large_bodies_are_refused_before_being_read(self):
        status, body = post("/api/recordings", raw=b"{}", headers={"Content-Length": str(p2c.MAX_INGEST + 1)})
        self.assertEqual(status, 413)
        self.assertIn("limit", body["error"])

    def test_a_good_push_is_saved_and_queued(self):
        status, body = post("/api/recordings", push())
        self.assertEqual((status, body), (202, {"queued": True, "already_queued": False}))
        self.assertEqual(self.queued(), [("local", "loc1", False)])
        saved = json.loads(p2c.local_file("loc1").read_text(encoding="utf-8"))
        self.assertEqual(saved["transcript"], "Line one.\nLine two.")
        self.assertEqual(saved["source"], "plaude on chris-pc")
        self.assertTrue(saved["received_at"])
        status, body = post("/api/recordings", push(name="Standup, renamed"))  # pushed again while still queued
        self.assertEqual((status, body), (202, {"queued": True, "already_queued": False}))
        self.assertEqual(json.loads(p2c.local_file("loc1").read_text(encoding="utf-8"))["name"], "Standup, renamed")

    def test_a_push_still_queued_is_not_queued_twice(self):
        post("/api/recordings", push())
        status, body = post("/api/recordings", push())
        self.assertEqual((status, body), (202, {"queued": False, "already_queued": True}))
        self.assertEqual(self.queued(), [("local", "loc1", False)])

    def test_a_recording_already_in_claude_is_reported_not_queued(self):
        state = p2c.load()
        state["sent"]["loc1"] = {"session_url": "https://claude.ai/code/session_01Y", "at": "2026-10-22T10:00:00", "local": True}
        p2c.save(state)
        status, body = post("/api/recordings", push())
        self.assertEqual((status, body), (200, {"status": "already_sent", "session_url": "https://claude.ai/code/session_01Y"}))
        self.assertEqual(self.queued(), [])
        self.assertFalse(p2c.local_file("loc1").exists())
        status, body = post("/api/recordings", push(again=True))
        self.assertEqual((status, body["queued"]), (202, True))
        self.assertEqual(self.queued(), [("local", "loc1", True)])
        self.assertTrue(p2c.local_file("loc1").exists())

    def test_the_check_trigger_is_untouched(self):
        p2c.CHECK_TOKEN = "c" * 24
        try:
            status, body = post("/api/check", raw=b"", token="c" * 24)
            self.assertEqual((status, body["queued"]), (202, True))
            status, _ = post("/api/check", raw=b"", token=TOKEN)  # the ingest token opens only its own door
            self.assertEqual(status, 403)
        finally:
            p2c.CHECK_TOKEN = ""


class LocalJob(LocalCase):
    def run_job(self, rid="loc1", again=False):
        p2c.bridge._run(("local", rid, again))
        return p2c.bridge

    def test_the_local_job_sends_through_claude_without_plaud(self):
        p2c.save_local(push())
        bridge = self.run_job()
        self.assertEqual(len(Fake.texts), 1)
        text = Fake.texts[0]
        self.assertIn("Recording: Standup\n", text)
        when = p2c.pa.recorded_at({"start_at": "2026-10-22T14:03:00-04:00"})
        self.assertIn(f"Recorded: {when:%Y-%m-%d %H:%M}\n", text)
        self.assertIn("Length: 1m01s\n", text)
        self.assertIn("Plaud file ID: loc1\n", text)
        self.assertIn("===== Local transcript =====\nTranscribed by plaude on chris-pc with whisper small. Plaud's cloud was not used: "
                      "there is no Plaud summary, and the Plaud connector cannot fetch this recording.", text)
        self.assertIn("===== Transcript =====\n[00:00 - 00:02] Line one.\n[00:02 - 00:05] Line two.\n", text)
        sent = p2c.load()["sent"]["loc1"]
        self.assertEqual((sent["session_url"], sent["name"], sent["local"], sent["with_summary"]),
                         ("https://claude.ai/code/session_01Y", "Standup", True, True))
        self.assertEqual(bridge.status["loc1"], "sent")
        self.assertIn("→ Standup · https://claude.ai/code/session_01Y", bridge.log[-1])

    def test_without_segments_the_text_goes_as_it_came(self):
        p2c.save_local(push(segments=..., source=..., model=...))
        self.run_job()
        text = Fake.texts[0]
        self.assertIn("===== Local transcript =====\nTranscribed locally. Plaud's cloud", text)
        self.assertIn("===== Transcript =====\nLine one.\nLine two.\n", text)

    def test_a_push_ignores_the_setup_date_but_not_a_send_already_made(self):
        state = p2c.load()
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        self.assertEqual(p2c.decide(rec(minutes_ago=3 * 1440), state, "t", ["n"], False, time.time()), "before")  # Plaud's own
        p2c.save_local(push(recorded_at=old))
        self.run_job()
        self.assertEqual(len(Fake.texts), 1)
        bridge = self.run_job()  # pushed again without `again`
        self.assertEqual(len(Fake.texts), 1)
        self.assertIn("– Standup is already in Claude; not sent again", bridge.log[-1])
        self.assertEqual(bridge.status["loc1"], "sent")
        self.run_job(again=True)
        self.assertEqual(len(Fake.texts), 2)

    def test_a_send_that_needs_a_look_waits_for_again(self):
        state = p2c.load()
        state["failed"]["loc1"] = {"error": "Claude returned 401", "retryable": False, "at": datetime.now().isoformat()}
        p2c.save(state)
        p2c.save_local(push())
        bridge = self.run_job()
        self.assertEqual(Fake.texts, [])
        self.assertEqual(bridge.status["loc1"], "failed")
        self.assertIn("needs a look first", bridge.log[-1])
        state = p2c.load()
        state["failed"]["loc1"]["retryable"] = True  # a rate limit: sent on its own next time
        p2c.save(state)
        self.run_job()
        self.assertEqual(len(Fake.texts), 1)
        self.assertTrue(p2c.load()["sent"]["loc1"]["local"])
        self.assertNotIn("loc1", p2c.load()["failed"])

    def test_a_failed_local_send_is_marked_local(self):
        p2c.save_local(push())
        state = p2c.load()
        state["not_before"] = time.time() + 600
        p2c.save(state)
        bridge = self.run_job()
        self.assertEqual(Fake.texts, [])
        failure = p2c.load()["failed"]["loc1"]
        self.assertEqual((failure["local"], failure["retryable"]), (True, True))
        self.assertEqual(bridge.status["loc1"], "failed")

    def test_a_missing_file_is_reported_not_crashed(self):
        bridge = self.run_job("gone")
        self.assertEqual(bridge.status["gone"], "unreadable")
        self.assertIn("couldn't read its local transcript file", bridge.log[-1])
        p2c.LOCAL.mkdir(parents=True, exist_ok=True)
        p2c.local_file("torn").write_text("{half", encoding="utf-8")
        self.assertEqual(self.run_job("torn").status["torn"], "unreadable")
        self.assertEqual(Fake.texts, [])

    def test_a_push_reaches_claude_end_to_end(self):
        """The real worker thread: HTTP push in, fire out, nothing asked of Plaud."""
        p2c.bridge = p2c.Bridge()
        status, body = post("/api/recordings", push("e2e"))
        self.assertEqual((status, body["queued"]), (202, True))
        for _ in range(100):
            if Fake.texts:
                break
            time.sleep(0.05)
        self.assertEqual(len(Fake.texts), 1)
        self.assertIn("Plaud file ID: e2e\n", Fake.texts[0])
        self.assertTrue(p2c.load()["sent"]["e2e"]["local"])
        status, body = post("/api/recordings", push("e2e"))
        self.assertEqual((status, body["status"]), (200, "already_sent"))


class Page(LocalCase):
    def test_local_recordings_are_listed_with_a_marker_even_without_plaud(self):
        body, meta = p2c.page_body("console", None)
        self.assertIn("Recordings appear once Plaud is signed in, or when a collector pushes a local transcript.", body)
        p2c.save_local(push())
        body, meta = p2c.page_body("console", None)
        self.assertIn("Recordings · last 3 days · 1 local", body)
        self.assertIn("Standup", body)
        self.assertIn('title="Transcribed plaude on chris-pc · whisper small">Local</span>', body)
        self.assertIn(">Received<", body)
        self.assertIn('<input type="hidden" name="id" value="loc1">', body)
        self.assertIn("<td class=\"hide-sm\">1m01s</td>", body)
        p2c.bridge._run(("local", "loc1", False))
        body, meta = p2c.page_body("console", None)
        self.assertIn(">In Claude<", body)
        self.assertIn('href="https://claude.ai/code/session_01Y"', body)
        self.assertIn(">Send again<", body)

    def test_the_list_mixes_plaud_and_local_recordings_newest_first(self):
        p2c.signin.page_state = lambda: dict(PAGE_STATE, state="signed_in", who="tester@example.com")
        plaud_rec = rec("r1", minutes_ago=30)
        p2c.plaud.recordings = lambda: [plaud_rec]
        try:
            ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
            p2c.save_local(push("newer", recorded_at=ten_min_ago))
            p2c.save_local(push("undated", recorded_at=...))  # no date: it is listed by when it arrived (now)
            body, _ = p2c.page_body("console", None)
            self.assertLess(body.index('value="undated"'), body.index('value="newer"'))  # just received: at the top
            self.assertLess(body.index('value="newer"'), body.index('value="r1"'))
            self.assertNotIn("Recordings appear once", body)
        finally:
            del p2c.plaud.recordings

    def test_send_on_the_page_runs_the_local_job_for_a_pushed_id(self):
        p2c.save_local(push())
        status, _ = post("/send", raw=b"id=loc1", token=None, ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        status, _ = post("/send", raw=b"id=loc1&again=2026-10-22T10:00:00", token=None, ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        status, _ = post("/send", raw=b"id=plaud1", token=None, ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 303)
        self.assertEqual(self.queued(), [("local", "loc1", False), ("local", "loc1", True), ("send", "plaud1", "")])


if __name__ == "__main__":
    unittest.main()
