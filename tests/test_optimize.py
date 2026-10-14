"""`devtime analyze` / `optimize list | apply | remove`, review-context size.

    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from devtime import __main__ as cli, optimize, paths, setup, tick  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        changes = os.path.join(self.dir, "config", "changes.toml")
        for patcher in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir, "DEVTIME_NO_SERVER_START": "1"}),
                        mock.patch.object(paths, "DATA", self.dir),
                        mock.patch.object(paths, "CACHE", os.path.join(self.dir, "cache")),
                        mock.patch("devtime.overview.CHANGES", changes)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.md = os.path.join(self.dir, "CLAUDE.md")
        self.settings = os.path.join(self.dir, "settings.json")
        self.changes = changes

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(list(argv))
        return out.getvalue()

    def read(self, path):
        with open(path, "rb") as f:
            return f.read().decode("utf-8")

    def write(self, path, text):
        with open(path, "wb") as f:
            f.write(text.encode("utf-8"))


class Catalogue(unittest.TestCase):
    def test_parses(self):
        items = optimize.load_catalogue()
        ids = [i["id"] for i in items]
        self.assertEqual(len(ids), len(set(ids)))
        for expected in ("slim-agents", "runner-for-long-jobs", "readonly-on-haiku", "handoff-250k",
                         "autocompact-400k", "scripts-first", "parallelism", "sonnet-default-coding", "weekly-cap",
                         "background-not-polling"):
            self.assertIn(expected, ids)
        for item in items:
            self.assertTrue(item["apply"], item["id"])
            for a in item["apply"]:
                if a["kind"] == "claude_md":
                    text = a["text"]
                    self.assertNotRegex(text, r"[A-Za-z]:[\\/]")
                elif a["kind"] == "settings_env":
                    self.assertTrue(a["key"] and a["value"])
        env = [a for i in items for a in i["apply"] if a["kind"] == "settings_env"]
        self.assertEqual((env[0]["key"], env[0]["value"]), ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "400000"))


class ApplyRemove(Base):
    def test_claude_md_apply_dry_run_remove(self):
        original = "# Mine\n\nKeep this.\n"
        self.write(self.md, original)
        out = self.run_cli("optimize", "apply", "slim-agents", "--dry-run")
        self.assertIn("+<!-- devtime:opt:slim-agents -->", out)
        self.assertEqual(self.read(self.md), original)
        self.assertFalse(os.path.exists(self.changes))
        out = self.run_cli("optimize", "apply", "slim-agents")
        self.assertIn("Applied Slim agent types", out)
        text = self.read(self.md)
        self.assertTrue(text.startswith(original))
        self.assertIn("<!-- devtime:opt-end:slim-agents -->", text)
        self.assertIn("devtime:coder", text)
        self.assertIn("Applied Slim agent types", self.read(self.changes))
        again = self.run_cli("optimize", "apply", "slim-agents")  # idempotent, no second marker
        self.assertEqual(self.read(self.md), text)
        self.assertIn('"marker":null', again)
        self.run_cli("optimize", "remove", "slim-agents")
        self.assertEqual(self.read(self.md), original)
        self.assertIn("Removed Slim agent types", self.read(self.changes))

    def test_claude_md_created_and_crlf_kept(self):
        self.run_cli("optimize", "apply", "parallelism")
        self.assertIn("devtime:opt:parallelism", self.read(self.md))
        self.write(self.md, "a\r\nb\r\n")
        self.run_cli("optimize", "apply", "parallelism")
        text = self.read(self.md)
        self.assertNotIn("\r\n\r\r", text)
        self.assertEqual(text.count("\n"), text.count("\r\n"))
        self.run_cli("optimize", "remove", "parallelism")
        self.assertEqual(self.read(self.md), "a\r\nb\r\n")

    def test_settings_env_preserves_keys_and_backs_up(self):
        original = json.dumps({"model": "opus", "env": {"FOO": "1"}, "hooks": {"Stop": []}}, indent=4) + "\n"
        self.write(self.settings, original)
        out = self.run_cli("optimize", "apply", "autocompact-400k", "--dry-run")
        self.assertIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", out)
        self.assertEqual(self.read(self.settings), original)
        self.assertFalse(os.path.exists(self.settings + ".bak"))
        self.run_cli("optimize", "apply", "autocompact-400k")
        data = json.loads(self.read(self.settings))
        self.assertEqual(data["env"], {"FOO": "1", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"})
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"], {"Stop": []})
        self.assertEqual(self.read(self.settings + ".bak"), original)
        self.assertIn('\n    "model"', self.read(self.settings))  # indent kept
        self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertEqual(json.loads(self.read(self.settings)), json.loads(original))

    def test_settings_without_file_and_broken_file(self):
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.assertEqual(json.loads(self.read(self.settings)), {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}})
        self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertEqual(json.loads(self.read(self.settings)), {})
        self.write(self.settings, "{ not json")
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "apply", "autocompact-400k")
        self.assertEqual(self.read(self.settings), "{ not json")

    def test_note_and_unknown(self):
        out = self.run_cli("optimize", "apply", "weekly-cap")
        self.assertIn("/devtime:start", out)
        self.assertFalse(os.path.exists(self.md))
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "apply", "nope")

    def test_list_and_applied_state(self):
        self.run_cli("optimize", "apply", "handoff-250k")
        self.run_cli("optimize", "apply", "autocompact-400k")
        out = self.run_cli("optimize", "list")
        self.assertRegex(out, r"handoff-250k\s+devtime")
        self.assertRegex(out, r"autocompact-400k\s+devtime")
        self.assertRegex(out, r"slim-agents\s+-")
        self.write(self.md, "I tell agents: scripts where possible.\n")
        self.assertRegex(self.run_cli("optimize", "list"), r"scripts-first\s+manual")


class RealData(unittest.TestCase):
    """Uses the machine's own transcripts and cache (read-only apart from the cache), a temp Claude dir for the files."""

    def test_shape_on_real_data(self):
        projects = os.path.expanduser("~/.claude/projects")
        if not os.path.isdir(projects) or not os.listdir(projects):
            self.skipTest("no transcripts on this machine")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        from devtime.store import Store
        real = Store(verbose=False)
        try:
            real.refresh()
        except PermissionError:
            self.skipTest("transcript cache busy")
        out = optimize.analyze(7, store=real)
        text = json.dumps(out, separators=(",", ":"))
        self.assertLess(len(text), 3500)
        for key in ("weeklyUsd", "byModel", "byRole", "baseCtxByType", "big", "waiting", "readonlyBig", "concurrent",
                    "autoCompactWindow", "recurringFailures", "catalogue"):
            self.assertIn(key, out)
        ids = [c["id"] for c in out["catalogue"]]
        self.assertEqual(sorted(ids), sorted(i["id"] for i in optimize.load_catalogue()))
        for c in out["catalogue"]:
            self.assertEqual(set(c), {"id", "applied", "by", "estWeeklyUsd", "why"})
            self.assertIsInstance(c["applied"], bool)
        ests = [c["estWeeklyUsd"] for c in out["catalogue"]]
        known = [e for e in ests if e is not None]
        self.assertEqual(known, sorted(known, reverse=True))
        self.assertEqual(ests[:len(known)], known)  # null estimates last
        for share in ("over250kShare", "over400kShare"):
            self.assertTrue(0 <= out["big"][share] <= 1)


class Analyze(Base):
    def test_applied_flag_follows_files(self):
        data = {"transcripts": [], "iv": {"t": [], "s": [], "e": [], "k": [], "l": [], "f": []},
                "tok": {"i": [], "m": [], "in": [], "out": [], "cr": [], "cw5": [], "cw1": [], "cost": []},
                "kinds": [], "labels": [], "models": [], "waiting": [], "flags": {"error": 2, "wait": 32}}
        store = mock.Mock()
        store.slice.return_value = data
        store.config = mock.Mock()
        store.config.price.return_value = None
        self.run_cli("optimize", "apply", "autocompact-400k")
        out = optimize.analyze(7, store=store)
        by_id = {c["id"]: c for c in out["catalogue"]}
        self.assertTrue(by_id["autocompact-400k"]["applied"])
        self.assertFalse(by_id["slim-agents"]["applied"])
        self.assertEqual(out["weeklyUsd"], 0)


class ReviewContext(unittest.TestCase):
    def test_trim_watch_caps_subagents(self):
        subs = [{"session": "S", "model": "opus-5", "live": False, "costRecent": 1.0, "context": 123456,
                 "calls": {"a": 1, "b": 2, "c": 3, "d": 4}, "description": "x" * 200, "agentType": "coder",
                 "cost": float(i), "minutes": 5, "edits": 1, "commits": 0} for i in range(60)]
        watch = {"hours": 2, "mains": [], "subagents": subs, "alerts": [], "monitoring": {},
                 "experiments": {"ab": [], "runner": [], "types": [{"type": "t"}] * 50}}
        out = tick.trim_watch(watch)
        self.assertEqual(len(out["subagents"]), 25)
        self.assertEqual(out["subagents"][0]["cost"], 59.0)
        self.assertEqual(len(out["subagents"][0]["desc"]), 50)
        self.assertEqual(len(out["subagents"][0]["calls"]), 3)
        self.assertLess(len(json.dumps(out, separators=(",", ":"))), 8000)


if __name__ == "__main__":
    unittest.main()
