"""`devtime setup`: CLAUDE.md block and hook-duplicate detection.

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
from devtime import setup  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.md = os.path.join(self.tmp.name, "CLAUDE.md")

    def run_md(self, cap, dry_run=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.claude_md_cmd(cap, dry_run)
        return out.getvalue()

    def read(self, path=None):
        with open(path or self.md, "rb") as f:
            return f.read().decode("utf-8")


class ClaudeMd(Base):
    def test_insert_into_missing_file(self):
        out = self.run_md(85)
        self.assertEqual(json.loads(out.splitlines()[0])["action"], "created")
        text = self.read()
        self.assertTrue(text.startswith(setup.START) and text.rstrip().endswith(setup.END))
        self.assertIn("at 85 % of the weekly", text)

    def test_insert_into_existing_file(self):
        original = "# Mine\n\nKeep  this\r\nexactly.\n"
        with open(self.md, "wb") as f:
            f.write(original.encode())
        self.run_md(90)
        text = self.read()
        self.assertTrue(text.startswith(original))
        self.assertEqual(text.count(setup.START), 1)
        self.assertIn("at 90 % of the weekly", text)

    def test_replace_is_idempotent(self):
        with open(self.md, "wb") as f:
            f.write(b"before\n\n")
        self.run_md(85)
        first = self.read()
        self.run_md(85)
        self.assertEqual(self.read(), first)
        with open(self.md, "ab") as f:
            f.write(b"\nafter\n")
        self.run_md(70)
        text = self.read()
        self.assertEqual(text.count(setup.START), 1)
        self.assertIn("at 70 % of the weekly", text)
        self.assertNotIn("at 85 %", text)
        self.assertTrue(text.startswith("before\n") and text.endswith("\nafter\n"))
        self.run_md(70)
        self.assertEqual(self.read(), text)

    def test_dry_run_writes_nothing(self):
        out = self.run_md(85, dry_run=True)
        self.assertFalse(os.path.exists(self.md))
        self.assertIn("+## Usage tracking (devtime)", out)
        with open(self.md, "wb") as f:
            f.write(b"x\n")
        self.run_md(85, dry_run=True)
        self.assertEqual(self.read(), "x\n")


class Check(Base):
    def test_duplicate_hooks(self):
        plugin = setup.ROOT
        settings = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}, "hooks": {
            "PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "python /old/devtime/hooks/agent_budget.py"},
                {"type": "command", "command": "python " + plugin + "/hooks/agent_budget.py"},
                {"type": "command", "command": "python other.py"}]}],
            "SessionStart": [{"hooks": [
                {"type": "command", "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" ensure_server.py'},
                {"type": "command", "command": "python C:\\x\\hooks\\ensure_server.py"}]}]}}
        path = os.path.join(self.tmp.name, "settings.json")
        with open(path, "w") as f:
            json.dump(settings, f)
        found = setup.find_hook_duplicates(settings)
        self.assertEqual([(d["event"], d["command"]) for d in found],
                         [("PreToolUse", "python /old/devtime/hooks/agent_budget.py"),
                          ("SessionStart", "python C:\\x\\hooks\\ensure_server.py")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = setup.check(projects=False)
        self.assertEqual(len(result["settings"]["duplicateHooks"]), 2)
        self.assertEqual(result["settings"]["autoCompactWindow"], "400000")
        self.assertFalse(result["claudeMd"]["block"])
        self.run_md(80)
        with contextlib.redirect_stdout(io.StringIO()):
            result = setup.check(projects=False)
        self.assertEqual(result["claudeMd"]["cap"], 80.0)
        self.assertEqual(json.loads(out.getvalue())["settings"]["path"], path)



class FiveHourCap(unittest.TestCase):
    def test_block_carries_both_caps(self):
        from devtime import setup
        text, _ = setup.new_claude_md(None, 85, 95)
        self.assertIn("at 85 % of the weekly", text)
        self.assertIn("at 95 % of the 5-hour", text)
        self.assertEqual(setup.block_caps(text), (85.0, 95.0))
        text, _ = setup.new_claude_md(text, 90)
        self.assertEqual(setup.block_caps(text), (90.0, None))


if __name__ == "__main__":
    unittest.main()
