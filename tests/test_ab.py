"""/devtime:ab: arm hashing, new / stop / CLAUDE.md block, report (rework detection), launcher, hook enforcement.

    python -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import __main__ as cli, ab, paths  # noqa: E402


def load_hook():
    spec = importlib.util.spec_from_file_location("agent_budget_under_test", os.path.join(ROOT, "hooks", "agent_budget.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        for patcher in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir, "DEVTIME_NO_SERVER_START": "1"}),
                        mock.patch.object(paths, "DATA", self.dir),
                        mock.patch.object(paths, "CACHE", os.path.join(self.dir, "cache")),
                        mock.patch.object(paths, "USER_CONFIG", os.path.join(self.dir, "config"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.md = os.path.join(self.dir, "CLAUDE.md")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(list(argv))
        return out.getvalue()

    def new(self, *extra):
        return self.run_cli("ab", "new", "sdc", "--title", "Sonnet vs Opus", "--arm", "sonnet:sonnet:devtime:coder",
                            "--arm", "opus:opus:devtime:coder", *extra)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read().decode("utf-8")


class Arms(unittest.TestCase):
    def test_deterministic_and_known_value(self):
        a = [ab.arm_index("exp", "stage-%d" % i, 2) for i in range(50)]
        self.assertEqual(a, [ab.arm_index("exp", "stage-%d" % i, 2) for i in range(50)])
        self.assertEqual(ab.arm_index("EXP", "Stage-1", 2), ab.arm_index("exp", "stage-1", 2))  # case-insensitive
        import hashlib
        self.assertEqual(ab.arm_index("x", "y", 3), int(hashlib.sha1(b"x:y").hexdigest(), 16) % 3)

    def test_spread(self):
        for n in (2, 3):
            counts = [0] * n
            for i in range(300):
                counts[ab.arm_index("sdc", "stage%d" % i, n)] += 1
            self.assertGreater(min(counts), 300 / n * 0.7, counts)

    def test_hook_hash_matches(self):
        hook = load_hook()
        for i in range(40):
            self.assertEqual(hook.arm_index("sdc", "s%d" % i, 3), ab.arm_index("sdc", "s%d" % i, 3))

    def test_stem(self):
        self.assertEqual(ab.stem_of("view-toggle2"), "view-toggle")
        self.assertEqual(ab.stem_of("view-toggle-2"), "view-toggle")
        self.assertEqual(ab.stem_of("r12"), "r")
        self.assertIsNone(ab.stem_of("view-toggle"))


class NewStop(Base):
    def test_new_arm_stop(self):
        with open(self.md, "w", encoding="utf-8", newline="") as f:
            f.write("# mine\n\nkeep me\n")
        dry = self.new("--dry-run")
        self.assertIn("devtime:ab:sdc", dry)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "config", "experiments.toml")))
        self.assertNotIn("devtime:ab", self.read(self.md))
        self.new("--instructions", "opus=think hard")
        text = self.read(self.md)
        self.assertIn("<!-- devtime:ab:sdc -->", text)
        self.assertIn("<!-- devtime:ab-end:sdc -->", text)
        self.assertIn("devtime.py", text)
        self.assertIn("AB:sdc:<stage-slug>", text)
        self.assertTrue(text.startswith("# mine\n\nkeep me\n"))
        exps = ab.load_experiments()
        self.assertEqual(exps[0]["arms"][0], {"name": "sonnet", "model": "sonnet", "agent_type": "devtime:coder"})
        self.assertEqual(exps[0]["arms"][1]["instructions"], "think hard")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "devtime.py")))
        line = self.run_cli("ab", "arm", "sdc", "view-toggle").strip()
        self.assertIn('description must start "AB:sdc:view-toggle "', line)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("ARM " + ab.arm_for(exps[0], "view-toggle")["name"], line)
        with self.assertRaises(SystemExit):
            self.new()  # exists
        with mock.patch.object(ab, "gather", return_value=[]):
            out = self.run_cli("ab", "stop", "sdc")
        self.assertIn("removed", out)
        self.assertEqual(self.read(self.md), "# mine\n\nkeep me\n")
        self.assertFalse(ab.load_experiments()[0]["active"])
        self.assertIn("not active", self.run_cli("ab", "arm", "sdc", "x"))
        self.assertIn("stopped", self.run_cli("ab", "list"))

    def test_needs_two_arms(self):
        with self.assertRaises(SystemExit):
            self.run_cli("ab", "new", "x", "--title", "t", "--arm", "only")

    def test_toml_round_trip_quotes(self):
        exp = {"id": "q", "title": 'He said "hi" \\ there', "active": True, "started": "2026-10-14", "opt": None,
               "arms": [{"name": "a", "model": "m", "instructions": 'use "x"\nthen y'}, {"name": "b"}]}
        path = os.path.join(self.dir, "e.toml")
        ab.save_experiments([exp], path)
        back = ab.load_experiments(path)[0]
        self.assertEqual(back["title"], exp["title"])
        self.assertEqual(back["arms"], exp["arms"])

    def test_launcher_runs_cli(self):
        launcher = paths.install_launcher(ROOT)
        self.assertEqual(self.read(os.path.join(self.dir, "plugin_root.txt")).strip(), ROOT)
        self.new()
        env = dict(os.environ, DEVTIME_HOME=self.dir)
        result = subprocess.run([sys.executable, launcher, "ab", "arm", "sdc", "view-toggle"], capture_output=True,
                                text=True, env=env, cwd=self.dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARM ", result.stdout)
        self.assertIn("AB:sdc:view-toggle", result.stdout)
        result = subprocess.run([sys.executable, launcher, "ab", "report", "nope"], capture_output=True, text=True,
                                env=env, cwd=self.dir)
        self.assertNotEqual(result.returncode, 0)


def row(desc, start, end=None, section="", model="sonnet", cost=1.0, commits=1, edits=3, errors=0, out=1000, type_=""):
    return {"id": desc + str(start), "start": start, "end": end if end is not None else start + 600, "desc": desc,
            "type": type_, "model": model, "cost": cost, "commits": commits, "edits": edits, "errors": errors,
            "out": out, "section": section}


class Report(unittest.TestCase):
    exp = {"id": "sdc", "title": "t", "active": True, "started": "2026-10-14", "opt": None,
           "arms": [{"name": "sonnet", "model": "sonnet"}, {"name": "opus", "model": "opus"}]}

    def slug(self, arm, taken=()):
        i = 0
        while True:
            name = "st%d" % i
            if name not in taken and ab.arm_for(self.exp, name)["name"] == arm:
                return name
            i += 1

    def test_rework_detection(self):
        day = 86400
        same = self.slug("sonnet")
        sec = self.slug("sonnet", (same,))
        clean = self.slug("sonnet", (same, sec))
        late = self.slug("opus")
        stem = "view-toggle"
        rows = [
            row("AB:sdc:%s build it" % same, 1000), row("AB:sdc:%s review notes" % same, 5000),
            row("AB:sdc:%s build it" % sec, 1000, section="Sec A"),
            row("Fix lint after review", 4000, section="Sec A"),                      # untagged fix, same section
            row("AB:sdc:%s build it" % clean, 1000, section="Sec B"),
            row("Write docs", 4000, section="Sec B"),                                  # same section, not a fix
            row("Fix something else", 4000, section="Sec C"),                          # a fix, other section
            row("AB:sdc:%s build it" % stem, 1000), row("AB:sdc:%s2 again" % stem, 3000),
            row("AB:sdc:%s build it" % late, 1000, section="Sec D"),
            row("Fix it", 1000 + 3 * day, section="Sec D"),                            # outside the 24 h window
            row("AB:other:x thing", 1000), row("no tag", 1000),
        ]
        rep = ab.build_report(self.exp, rows)
        by = {s["stage"]: s for s in rep["stageList"]}
        self.assertEqual(by[same]["reasons"], ["same-tag"])
        self.assertEqual(by[sec]["reasons"], ["section-fix"])
        self.assertFalse(by[clean]["reworked"])
        self.assertFalse(by[late]["reworked"])
        self.assertEqual(by[stem]["reasons"], ["stem+digit"])
        self.assertNotIn(stem + "2", by)  # merged into its base stage
        self.assertEqual(rep["stages"], 5)
        self.assertEqual(by[same]["agents"], 2)
        self.assertAlmostEqual(by[same]["cost"], 2.0)

    def test_outcomes_and_caveats(self):
        a, b = self.slug("sonnet"), self.slug("opus")
        rows = [row("AB:sdc:%s x" % a, 100, cost=2.0, commits=2, edits=5, errors=3, out=4000),
                row("AB:sdc:%s y" % b, 100, cost=6.0, commits=0, edits=1, errors=0, out=500, model="sonnet")]
        rep = ab.build_report(self.exp, rows)
        arms = {x["arm"]: x for x in rep["arms"]}
        self.assertEqual(arms["sonnet"]["commits"], 2)
        self.assertAlmostEqual(arms["sonnet"]["costPerCommit"], 1.0)
        self.assertIsNone(arms["opus"]["costPerCommit"])
        self.assertAlmostEqual(arms["sonnet"]["errorsPerStage"], 3.0)
        self.assertEqual(arms["opus"]["offArm"], 1)  # an opus-arm stage that ran on sonnet stays in the opus arm
        text = "\n".join(rep["notes"])
        self.assertIn("too few stages to call", text)
        self.assertIn("different model", text)
        self.assertIn("$/commit", ab.format_report(rep))

    def test_balanced_enough_stages_not_flagged(self):
        rows = []
        for arm in ("sonnet", "opus"):
            taken = []
            for _ in range(10):
                s = self.slug(arm, taken)
                taken.append(s)
                rows.append(row("AB:sdc:%s x" % s, 100, model=arm))
        rep = ab.build_report(self.exp, rows)
        self.assertNotIn("too few", "\n".join(rep["notes"]))
        self.assertNotIn("unbalanced", "\n".join(rep["notes"]))


class HookEnforcement(Base):
    def setUp(self):
        super().setUp()
        self.new()
        self.hook = load_hook()
        self.exp = ab.load_experiments()[0]

    def launch(self, stage, **inp):
        data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s1",
                "tool_input": dict({"description": "AB:sdc:%s do it" % stage}, **inp)}
        return self.hook.decide(data)

    def test_matching_arm_passes_and_wrong_arm_is_denied(self):
        stage = "view-toggle"
        arm = ab.arm_for(self.exp, stage)
        other = next(a for a in self.exp["arms"] if a is not arm)
        self.assertIsNone(self.launch(stage, model=arm["model"], subagent_type="devtime:coder"))
        denied = self.launch(stage, model=other["model"], subagent_type="devtime:coder")
        spec = denied["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        self.assertIn("arm '%s'" % arm["name"], spec["permissionDecisionReason"])
        self.assertIn(arm["model"], spec["permissionDecisionReason"])

    def test_missing_model_and_wrong_agent_type(self):
        stage = "view-toggle"
        arm = ab.arm_for(self.exp, stage)
        self.assertEqual(self.launch(stage, subagent_type="devtime:coder")["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.launch(stage, model=arm["model"], subagent_type="general-purpose")
                         ["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.launch(stage, model=arm["model"], subagent_type="coder"))  # plugin prefix optional

    def test_task_tool_name_also_checked(self):
        data = {"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_input": {"description": "AB:sdc:zz go"}}
        self.assertIsNotNone(self.hook.decide(data))

    def test_unknown_stopped_and_untagged_pass(self):
        self.assertIsNone(self.hook.decide({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                                            "tool_input": {"description": "AB:nope:zz go"}}))
        self.assertIsNone(self.hook.decide({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                                            "tool_input": {"description": "AB:R12 legacy tag"}}))
        self.assertIsNone(self.hook.decide({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                                            "tool_input": {"description": "plain work"}}))
        with mock.patch.object(ab, "gather", return_value=[]):
            self.run_cli("ab", "stop", "sdc")
        self.assertIsNone(self.launch("view-toggle"))

    def test_no_experiments_file(self):
        os.remove(os.path.join(self.dir, "config", "experiments.toml"))
        self.assertIsNone(self.launch("view-toggle"))


if __name__ == "__main__":
    unittest.main()
