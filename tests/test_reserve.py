"""The weekly reserve (usable = 100 - reserve x days to the reset), its CLAUDE.md block forms, the dashboard fields, and
that no devtime-injected instruction tells an agent to commit unconditionally.

    python -m unittest discover -s tests
"""

import contextlib
import glob
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import alerts, optimize, overview, paths, reserve, setup  # noqa: E402

OLD_BLOCK = """<!-- devtime:start -->
## Usage tracking (devtime)
- On long or autonomous runs, call `mcp__ccd_session_mgmt__get_usage` before launching each agent stage.
- Weekly cap: at 85 % of the weekly all-models limit start no new agents or stages; let running ones reach a safe point, commit, stop and tell the user what was left undone. Only the user raises the cap.
- 5-hour cap: at 95 % of the 5-hour limit start no new agents or stages; let running ones reach a safe point, then wait.
<!-- devtime:end -->"""


class Formula(unittest.TestCase):
    def test_usable_by_days_left(self):
        for days, want in ((7, 65), (3, 85), (1.5, 92.5), (0.5, 97.5), (1, 95)):
            self.assertAlmostEqual(reserve.usable(5, days), want)
        self.assertAlmostEqual(reserve.usable(10, 7), 30)
        self.assertAlmostEqual(reserve.usable(2, 1), 98)

    def test_clamped(self):
        self.assertEqual(reserve.usable(20, 7), 0.0)
        self.assertEqual(reserve.usable(5, -2), 100.0)  # past the reset
        self.assertEqual(reserve.usable(-5, 3), 100.0)

    def test_rule_from_settings(self):
        self.assertEqual(reserve.settings_rule({}), {"mode": "reserve", "value": 5.0})
        self.assertEqual(reserve.settings_rule({"weekly_reserve_per_day": 10}), {"mode": "reserve", "value": 10})
        self.assertEqual(reserve.settings_rule({"weekly_cap": 80, "weekly_reserve_per_day": 10}),
                         {"mode": "cap", "value": 80})

    def test_usable_now(self):
        now = 1_000_000.0
        self.assertAlmostEqual(reserve.usable_now({"mode": "reserve", "value": 5}, now + 1.5 * 86400, now), 92.5)
        self.assertIsNone(reserve.usable_now({"mode": "reserve", "value": 5}, None, now))
        self.assertEqual(reserve.usable_now({"mode": "cap", "value": 85}, None, now), 85)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.md = os.path.join(self.tmp.name, "CLAUDE.md")

    def write_md(self, text):
        with open(self.md, "wb") as f:
            f.write(text.encode("utf-8"))


class Block(Base):
    def test_reserve_render_and_parse(self):
        text, action = setup.new_claude_md(None, None, 95, per_day=10)
        self.assertEqual(action, "created")
        self.assertIn("keep 10 % of the weekly all-models limit per day left before the reset", text)
        self.assertIn("usable = 100 - 10 x days left: 30 % with a week left, 70 % with 3 days, 90 % on the last day", text)
        self.assertIn("ask first", text)
        self.assertEqual(setup.block_weekly(text), {"mode": "reserve", "value": 10.0, "legacy": False})
        self.assertEqual(setup.block_caps(text), (None, 95.0))

    def test_default_is_five_per_day(self):
        text, _ = setup.new_claude_md(None, None)
        self.assertIn("keep 5 % of the weekly", text)
        self.assertIn("65 % with a week left, 85 % with 3 days, 95 % on the last day", text)

    def test_fixed_cap_render_and_parse(self):
        text, _ = setup.new_claude_md(None, 85, 95)
        self.assertEqual(setup.block_weekly(text), {"mode": "cap", "value": 85.0, "legacy": False})
        self.assertEqual(setup.block_caps(text), (85.0, 95.0))
        self.assertNotIn("keep ", text.split("Name subagents")[0].split("Weekly cap")[1])

    def test_old_block_parses(self):
        self.assertEqual(setup.block_caps(OLD_BLOCK), (85.0, 95.0))
        self.assertEqual(setup.block_weekly(OLD_BLOCK), {"mode": "cap", "value": 85.0, "legacy": True})
        self.assertIsNone(setup.block_weekly("nothing here"))

    def test_old_block_is_replaced_by_reserve(self):
        self.write_md("before\n\n" + OLD_BLOCK + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            setup.check(projects=False)  # an old block does not break the check
            setup.claude_md_cmd(per_day=5)
        with open(self.md, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count(setup.START), 1)
        self.assertTrue(text.startswith("before\n"))
        self.assertEqual(setup.claude_md_weekly(), {"mode": "reserve", "value": 5.0})

    def test_check_reports_the_form(self):
        self.write_md(OLD_BLOCK + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            result = setup.check(projects=False)
        self.assertEqual(result["claudeMd"]["weekly"], {"mode": "cap", "value": 85.0, "legacy": True})
        self.assertEqual(result["claudeMd"]["cap"], 85.0)
        with contextlib.redirect_stdout(io.StringIO()):
            setup.claude_md_cmd(per_day=2)
            result = setup.check(projects=False)
        self.assertEqual(result["claudeMd"]["weekly"], {"mode": "reserve", "value": 2.0, "legacy": False})
        self.assertIsNone(result["claudeMd"]["cap"])

    def test_cli_flags(self):
        from devtime import __main__ as cli
        for argv, key, value in ((["setup", "claude-md", "--reserve-per-day", "10", "--dry-run"], "reservePerDay", "10"),
                                 (["setup", "claude-md", "--cap", "80", "--dry-run"], "cap", "80"),
                                 (["setup", "claude-md", "--dry-run"], "reservePerDay", "5")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(argv)
            self.assertEqual(json.loads(out.getvalue().splitlines()[0])[key], value)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["setup", "claude-md", "--cap", "80", "--reserve-per-day", "5"])

    def test_rule_prefers_the_block_over_settings(self):
        self.assertEqual(reserve.rule({"weekly_reserve_per_day": 3}), {"mode": "reserve", "value": 3})
        with contextlib.redirect_stdout(io.StringIO()):
            setup.claude_md_cmd(cap=80)
        self.assertEqual(reserve.rule({"weekly_reserve_per_day": 3}), {"mode": "cap", "value": 80.0})


class Dashboard(Base):
    NOW = 1_800_000_000.0
    DATA = {"transcripts": [], "org": paths.ORG, "kinds": [], "labels": [], "flags": {}, "tok": {"i": []},
            "iv": {"t": [], "s": [], "e": [], "k": [], "l": [], "f": []}}

    def sample(self, reset):
        return (self.NOW - 600, 0.1, 0.4, None, reset, paths.ORG)

    def test_tick_summary_weekly_usable(self):
        with mock.patch.object(overview, "plan_samples", return_value=[self.sample(self.NOW + 1.5 * 86400)]):
            out = overview.tick_summary(self.DATA, {}, [], self.NOW)
        self.assertEqual(out["weeklyUsable"], 92.5)
        self.assertEqual(out["reading"]["weekly"], 40.0)

    def test_tick_summary_rolls_a_passed_reset_forward(self):
        with mock.patch.object(overview, "plan_samples", return_value=[self.sample(self.NOW - 4 * 86400)]):
            out = overview.tick_summary(self.DATA, {"weekly_reserve_per_day": 10}, [], self.NOW)
        self.assertEqual(out["weeklyUsable"], 70.0)  # 3 days to the next reset

    def test_tick_summary_without_a_reset(self):
        with mock.patch.object(overview, "plan_samples", return_value=[]):
            self.assertIsNone(overview.tick_summary(self.DATA, {}, [], self.NOW)["weeklyUsable"])
            self.assertEqual(overview.tick_summary(self.DATA, {"weekly_cap": 80}, [], self.NOW)["weeklyUsable"], 80.0)

    def test_tick_command_carries_it(self):
        from devtime import tick
        numbers = {"reading": None, "labels": 0, "alerts": 0, "org": paths.ORG, "weeklyUsable": 85.0}
        with mock.patch.object(tick.setup, "ensure_server", return_value=("http://127.0.0.1:1/", False)), \
                mock.patch.object(tick, "tick_numbers", return_value=numbers):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                tick.tick()
        result = json.loads(out.getvalue())
        self.assertEqual(result["weeklyUsable"], 85.0)
        self.assertIn("weekly", result)


# Phrases that tell an agent to commit without a condition ("finish the step, commit, hand off", "commit if it is code").
UNCONDITIONAL = re.compile(r"(?i)\bcommit\s*,|,\s*commit\b|\bcommit if\b|\b(and|then|to) commit\b")


def load_hook():
    spec = importlib.util.spec_from_file_location("agent_budget_reserve", os.path.join(ROOT, "hooks", "agent_budget.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NoUnconditionalCommit(unittest.TestCase):
    def texts(self):
        hook = load_hook()
        yield "hook nudge", hook.nudge_text(300_000)
        yield "hook handoff", hook.HANDOFF
        for kind, advice in alerts.ADVICE.items():
            yield "alert advice: " + kind, advice
        for text in (setup.make_block(None, five=95), setup.make_block(85, five=95), setup.CAP_LINE, setup.RESERVE_LINE):
            yield "claude.md block", text
        for item in optimize.load_catalogue():
            for step in item.get("apply") or []:
                yield "catalogue " + item["id"], step.get("text") or ""
        for path in glob.glob(os.path.join(ROOT, "agents", "*.md")) + glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")):
            with open(path, encoding="utf-8") as f:
                yield os.path.relpath(path, ROOT), f.read()

    def test_no_text_says_to_commit_unconditionally(self):
        count = 0
        for where, text in self.texts():
            count += 1
            self.assertIsNone(UNCONDITIONAL.search(text), "%s: %s" % (where, UNCONDITIONAL.search(text)))
        self.assertGreater(count, 10)

    def test_the_rule_is_there(self):
        hook = load_hook()
        self.assertIn("never push", hook.nudge_text(300_000))
        self.assertIn("p4 submit", hook.nudge_text(300_000))
        self.assertIn("safe state", alerts.ADVICE["big context"])
        with open(os.path.join(ROOT, "agents", "coder.md"), encoding="utf-8") as f:
            coder = f.read()
        self.assertIn("p4 submit", coder)
        self.assertIn("Never push", coder)


if __name__ == "__main__":
    unittest.main()
