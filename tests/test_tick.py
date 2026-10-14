"""`devtime tick`, review-done / review-context and work-assign from stdin.

    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from devtime import __main__ as cli, paths, tick  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache")
        for target, value in ((paths, {"CACHE": self.cache, "DATA": self.tmp.name}),):
            patcher = mock.patch.multiple(target, **value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name, "DEVTIME_NO_SERVER_START": "1"})
        env.start()
        self.addCleanup(env.stop)

    def capture(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()


class Tick(Base):
    numbers = {"reading": {"weekly": 41.0, "fiveHour": 12.0, "ageMin": 7}, "labels": 3, "alerts": 1}

    def test_shape_server_unreachable(self):
        with mock.patch.object(tick.setup, "ensure_server", return_value=("http://127.0.0.1:1/", False)), \
                mock.patch.object(tick, "tick_numbers", return_value=self.numbers):
            out = json.loads(self.capture(tick.tick))
        self.assertEqual(set(out), {"url", "reading", "labels", "review", "alerts", "cap", "fiveHourCap", "setup", "server"})
        self.assertEqual(out["reading"]["weekly"], 41.0)
        self.assertEqual(out["labels"], 3)
        self.assertEqual(out["review"], {"due": True, "lastAt": None})
        self.assertIsNone(out["cap"])
        self.assertFalse(out["setup"])

    def test_server_numbers_and_claude_md(self):
        with open(os.path.join(self.tmp.name, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write(tick.setup.make_block(85))
        with mock.patch.object(tick.setup, "ensure_server", return_value=("http://x/", True)), \
                mock.patch.object(tick, "server_json", return_value=self.numbers):
            text = self.capture(tick.tick)
        out = json.loads(text)
        self.assertEqual((out["cap"], out["setup"], out["alerts"]), (85.0, True, 1))
        self.assertNotIn("server", out)
        self.assertLess(len(text), 600)

    def test_review_due(self):
        now = time.time()
        os.makedirs(self.cache)
        with open(tick.state_path(), "w") as f:
            json.dump({"lastAt": now - 3600}, f)
        self.assertFalse(tick.review_state(now)["due"])
        self.assertTrue(tick.review_state(now + 3700)["due"])


class Review(Base):
    def test_review_done_updates_state_and_log(self):
        self.capture(tick.review_done, "S1: use reader\n agents")
        self.capture(tick.review_done, "nothing to raise")
        with open(tick.log_path(), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("| S1: use reader agents"))
        self.assertFalse(tick.review_state(time.time())["due"])

    def test_review_context_has_watch_and_log_tail(self):
        for i in range(50):
            self.capture(tick.review_done, "line %d" % i)
        with mock.patch.object(tick, "watch_summary", return_value={"mains": []}) as w:
            out = json.loads(self.capture(tick.review_context, 2.0))
        w.assert_called_once_with(2.0)
        self.assertEqual(out["watch"]["mains"], [])
        self.assertEqual(out["watch"]["subagents"], [])
        self.assertEqual(len(out["reviewLog"]), tick.LOG_TAIL)
        self.assertTrue(out["reviewLog"][-1].endswith("line 49"))


class WorkAssignStdin(Base):
    def test_stdin(self):
        target = os.path.join(self.cache, "work_sections.json")
        import devtime.overview as overview
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({"abc": "Search", "def": "-"}).encode()), encoding="utf-8")
        with mock.patch.object(overview, "WORK_SECTIONS", target), \
                mock.patch.object(overview.assign_work, "__defaults__", (target,)), \
                mock.patch.object(overview.work_sections, "__defaults__", (target,)), \
                mock.patch.object(sys, "stdin", stdin):
            out = self.capture(cli.main, ["work-assign", "-"])
            saved = overview.work_sections(target)
        self.assertEqual(out.strip(), "assigned 2")
        self.assertEqual(saved, {"abc": "Search", "def": "-"})


if __name__ == "__main__":
    unittest.main()
