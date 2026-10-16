"""Readings posted by sessions (POST /api/reading): validation, org resolution, dedupe, stale readings, the queue.

Temp dirs only (the Fixture of test_orgs); the plan samples file is a temp file.

    python -m unittest discover -s tests
"""

import http.server
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_orgs  # noqa: E402
from devtime import orgs, overview, readings, server  # noqa: E402

ORG_A, ORG_B = test_orgs.ORG_A, test_orgs.ORG_B
NOW = time.time()


def body(session, weekly, five=10.0, reset="2026-10-20T00:00:00Z", five_reset="2026-10-16T20:00:00Z", at=None):
    return {"session": session, "at": at or time.time(), "fiveHour": five, "fiveHourReset": five_reset,
            "weekly": weekly, "weeklyReset": reset}


class Parse(unittest.TestCase):
    def test_valid(self):
        r = readings.parse(body("s1", 74.0, five=14.5), now=NOW)
        self.assertEqual((r["session"], r["fh"], r["sd"]), ("s1", 14.5, 74.0))
        self.assertEqual(r["sdReset"], overview.iso_ts("2026-10-20T00:00:00Z"))

    def test_rejects(self):
        for bad in (None, [], {}, {"session": "", "weekly": 5}, {"session": "s", "weekly": "5"},
                    {"session": "s", "weekly": True}, {"session": "s", "weekly": 5000},
                    {"session": "s", "fiveHour": 5}, {"session": 7, "weekly": 5}):
            self.assertIsNone(readings.parse(bad, now=NOW), bad)

    def test_clock_skew_uses_server_time(self):
        r = readings.parse(dict(body("s", 5), at=NOW - 86400), now=NOW)
        self.assertEqual(r["t"], round(NOW, 1))


class Fixture(test_orgs.Fixture):
    def setUp(self):
        super().setUp()
        self.samples = os.path.join(self.tmp.name, "samples.jsonl")  # never the real plan samples file
        self.store.readings.path = self.samples
        self.store.readings.last.clear()

    def rows(self):
        try:
            with open(self.samples, encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []


class Store(Fixture):
    def test_known_session_is_recorded_with_its_org_and_exact_resets(self):
        self.assertEqual(self.store.record_reading(body("sA", 43.0)), "recorded")
        (row,) = self.rows()
        self.assertEqual((row["org"], row["sd"], row["fh"]), (ORG_A, 43.0, 10.0))
        self.assertEqual(row["sdReset"], overview.iso_ts("2026-10-20T00:00:00Z"))
        samples = overview.plan_samples(self.store.config.settings, ORG_A, path=self.samples)
        self.assertIn(0.43, [round(s[2], 2) for s in samples])
        self.assertEqual([s[4] for s in samples if round(s[2], 2) == 0.43], [row["sdReset"]])

    def test_desktop_session_uses_the_desktop_index(self):
        self.assertEqual(self.store.record_reading(body("sD", 50.0)), "recorded")
        self.assertEqual(self.rows()[0]["org"], ORG_B)

    def test_identical_consecutive_readings_are_stored_once(self):
        self.assertEqual(self.store.record_reading(body("sA", 43.0)), "recorded")
        self.assertEqual(self.store.record_reading(body("sT", 43.0)), "duplicate")  # same org, same figures
        self.assertEqual(self.store.record_reading(body("sA", 44.0)), "recorded")
        self.assertEqual(self.store.record_reading(body("sB", 43.0)), "recorded")  # another org
        self.assertEqual([(r["org"], r["sd"]) for r in self.rows()], [(ORG_A, 43.0), (ORG_A, 44.0), (ORG_B, 43.0)])

    def test_stale_reading_is_dropped_but_a_new_week_is_kept(self):
        self.store.record_reading(body("sA", 60.0))
        self.assertEqual(self.store.record_reading(body("sT", 55.0)), "stale")  # an idle session's old figure
        self.assertEqual(self.store.record_reading(body("sT", 5.0, reset="2026-10-27T00:00:00Z")), "recorded")
        self.assertEqual([r["sd"] for r in self.rows()], [60.0, 5.0])

    def test_session_started_since_the_last_scan_is_found_by_its_transcript(self):
        test_orgs.transcript(os.path.join(self.projects, "projA", "sNew.jsonl"), "sNew", ORG_A, "New", ago=30)
        self.assertEqual(self.store.record_reading(body("sNew", 45.0)), "recorded")
        self.assertEqual(self.rows()[0]["org"], ORG_A)

    def test_unknown_session_is_queued_then_recorded_when_it_is_known(self):
        self.assertEqual(self.store.record_reading(body("sLater", 46.0)), "queued")
        self.assertEqual(self.rows(), [])
        self.write_desktop("claude-code-sessions", "acct", ORG_A, "local_9", "sLater")
        self.store._flush_readings()
        self.assertEqual([(r["org"], r["sd"]) for r in self.rows()], [(ORG_A, 46.0)])
        self.assertEqual(self.store.readings.pending, {})

    def test_queue_expires_to_unknown(self):
        self.store.record_reading(body("sNever", 47.0))
        self.assertEqual(self.store.readings.flush(lambda s: None, now=time.time() + 60), 0)
        self.assertEqual(self.store.readings.flush(lambda s: None, now=time.time() + readings.QUEUE_S + 1), 1)
        self.assertEqual(self.rows()[0]["org"], orgs.UNKNOWN)

    def test_session_ids_with_path_characters_are_not_looked_up(self):
        for bad in ("../sA", "a/b", "*", "x\y"):
            self.assertIsNone(self.store.session_org(bad))


def post(base, payload, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/api/reading", data=data, headers=h,
                                                           method="POST"), timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


class Endpoint(Fixture):
    def setUp(self):
        super().setUp()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.store))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def test_post_records(self):
        self.assertEqual(post(self.base, body("sA", 48.0)), (200, {"result": "recorded"}))
        self.assertEqual(post(self.base, body("sA", 48.0)), (200, {"result": "duplicate"}))
        self.assertEqual([r["sd"] for r in self.rows()], [48.0])

    def test_bad_requests(self):
        self.assertEqual(post(self.base, {"session": "sA"})[0], 400)
        self.assertEqual(post(self.base, None, raw=b"not json")[0], 400)
        self.assertEqual(post(self.base, body("sA", 48.0), headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(post(self.base, body("sA", 48.0), headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.rows(), [])

    def test_only_reading_accepts_posts(self):
        request = urllib.request.Request(self.base + "/api/tick", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=30)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
