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


class Ledger(Base):
    KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    DOC = ("# Mine\n\nIntro.\n\n## Rules\nBe brief.\n\n### Sub\nnested\n\n```\n## not a heading\n```\n\n"
           "## Other\nKeep.\n")

    def ledger(self):
        path = os.path.join(self.dir, "config", "installed.json")
        return json.loads(self.read(path)) if os.path.exists(path) else None

    def env(self):
        return json.loads(self.read(self.settings)).get("env")

    def test_restores_prior_different_value(self):
        original = json.dumps({"env": {self.KEY: "200000", "FOO": "1"}}, indent=2) + "\n"
        self.write(self.settings, original)
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.assertEqual(self.env()[self.KEY], "400000")
        entry = self.ledger()["items"][0]
        self.assertEqual(entry["id"], "autocompact-400k")
        self.assertEqual(entry["settings"], [{"key": "env." + self.KEY, "before": "200000", "after": "400000"}])
        self.assertTrue(entry["at"])
        out = self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertIn("restored", out)
        self.assertEqual(self.read(self.settings), original)
        self.assertEqual(self.ledger()["items"], [])

    def test_adopt_setting_makes_hand_set_value_removable(self):
        self.write(self.settings, json.dumps({"env": {self.KEY: "400000", "FOO": "1"}}) + "\n")
        self.run_cli("optimize", "adopt", "autocompact-400k", "--setting", "env." + self.KEY)
        self.assertEqual(self.env(), {self.KEY: "400000", "FOO": "1"})  # settings.json untouched
        self.assertIsNone(self.ledger()["items"][0]["settings"][0]["before"])
        self.assertIn("deleted", self.run_cli("optimize", "remove", "autocompact-400k"))
        self.assertEqual(self.env(), {"FOO": "1"})
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "adopt", "x", "--setting", "env.NOT_SET")

    def test_absent_key_is_deleted_and_env_dropped(self):
        self.write(self.settings, "{}\n")
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.assertIsNone(self.ledger()["items"][0]["settings"][0]["before"])
        self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertEqual(json.loads(self.read(self.settings)), {})

    def test_already_set_by_user_stays(self):
        original = json.dumps({"env": {self.KEY: "400000"}}) + "\n"
        self.write(self.settings, original)
        out = self.run_cli("optimize", "apply", "autocompact-400k")
        self.assertIn("already set by you", out)
        self.assertEqual(self.ledger()["items"][0]["settings"][0]["before"], "400000")
        self.assertFalse(os.path.exists(self.changes))  # nothing changed, no marker
        out = self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertIn("already set by you", out)
        self.assertEqual(self.env(), {self.KEY: "400000"})
        self.assertEqual(self.ledger()["items"], [])

    def test_changed_since_install_is_left_alone(self):
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.write(self.settings, json.dumps({"env": {self.KEY: "300000"}}))
        out = self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertIn("changed since install, left alone", out)
        self.assertEqual(self.env(), {self.KEY: "300000"})
        self.assertEqual(self.ledger()["items"], [])

    def test_no_ledger_entry_leaves_settings(self):
        self.write(self.settings, json.dumps({"env": {self.KEY: "400000"}}))
        out = self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertIn("no install record", out)
        self.assertEqual(self.env(), {self.KEY: "400000"})

    def test_apply_twice_keeps_first_before(self):
        self.write(self.settings, json.dumps({"env": {self.KEY: "1"}}))
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.assertEqual(self.ledger()["items"][0]["settings"][0]["before"], "1")
        self.run_cli("optimize", "remove", "autocompact-400k")
        self.assertEqual(self.env(), {self.KEY: "1"})

    def test_adopt_wraps_section_with_nested_headings(self):
        self.write(self.md, self.DOC)
        out = self.run_cli("optimize", "adopt", "my-rules", "--heading", "Rules", "--dry-run")
        self.assertIn("+<!-- devtime:opt:my-rules -->", out)
        self.assertEqual(self.read(self.md), self.DOC)
        self.assertIsNone(self.ledger())
        self.run_cli("optimize", "adopt", "my-rules", "--heading", "Rules")
        expected = (self.DOC.replace("## Rules", "<!-- devtime:opt:my-rules -->\n## Rules")
                    .replace("```\n\n## Other", "```\n<!-- devtime:opt-end:my-rules -->\n\n## Other"))
        self.assertEqual(self.read(self.md), expected)
        entry = self.ledger()["items"][0]
        self.assertEqual((entry["id"], entry["adopted"], entry["heading"]), ("my-rules", True, "Rules"))
        self.assertRegex(self.run_cli("optimize", "list"), r"my-rules\s+adopted")
        self.run_cli("optimize", "remove", "my-rules")
        self.assertEqual(self.read(self.md), "# Mine\n\nIntro.\n\n## Other\nKeep.\n")
        self.assertEqual(self.ledger()["items"], [])

    def test_adopt_refusals(self):
        self.write(self.md, self.DOC)
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "adopt", "x", "--heading", "Missing")
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "adopt", "Bad Id", "--heading", "Rules")
        self.run_cli("optimize", "adopt", "my-rules", "--heading", "Rules")
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "adopt", "other", "--heading", "Sub")  # already inside a devtime block
        with self.assertRaises(SystemExit):
            self.run_cli("optimize", "adopt", "other", "--heading", "Mine")  # contains a devtime block

    def test_adopt_catalogue_id_at_end_of_file(self):
        self.write(self.md, "# A\ntext\n\n## Scripts\nuse scripts")
        self.run_cli("optimize", "adopt", "scripts-first", "--heading", "## Scripts")
        self.assertRegex(self.run_cli("optimize", "list"), r"scripts-first\s+adopted")
        self.run_cli("optimize", "remove", "scripts-first")
        self.assertEqual(self.read(self.md), "# A\ntext\n")

    def test_remove_all(self):
        original_md = "# Mine\n\n## Hand\nwritten\n"
        self.write(self.md, original_md)
        self.write(self.settings, json.dumps({"env": {self.KEY: "200000"}}, indent=2) + "\n")
        original_settings = self.read(self.settings)
        self.run_cli("optimize", "apply", "slim-agents")
        self.run_cli("optimize", "apply", "autocompact-400k")
        self.run_cli("setup", "claude-md", "--cap", "85")
        self.write(self.md, self.read(self.md) + "\n## Adopt me\nstuff\n")
        self.run_cli("optimize", "adopt", "custom-one", "--heading", "Adopt me")
        text = self.read(self.md)
        for marker in ("devtime:opt:slim-agents", setup.START, "devtime:opt:custom-one"):
            self.assertIn(marker, text)
        self.assertRegex(self.run_cli("optimize", "list"), r"custom-one\s+adopted")
        out = self.run_cli("optimize", "remove", "all", "--dry-run")
        self.assertIn("-<!-- devtime:opt:slim-agents -->", out)
        self.assertEqual(self.read(self.md), text)  # a dry run changes nothing
        out = self.run_cli("optimize", "remove", "all")
        self.assertIn("Removed all devtime optimizations", out)
        self.assertEqual(self.read(self.md), original_md)
        self.assertEqual(self.read(self.settings), original_settings)
        self.assertEqual(self.ledger()["items"], [])
        self.assertIn("Removed all devtime optimizations", self.read(self.changes))


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
            self.assertEqual(set(c), {"id", "applied", "by", "estWeeklyUsd", "why", "guard"})
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
