"""Parallel support: settings.parallel / settings.features resolution, the hook's gating of the slot queue and the
agent budget rules (alerts and A/B stay on), the slot watcher, `setup parallel`, the catalogue's `requires`.

    python -m unittest tests.test_parallel
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402  (before devtime: a throw-away home, parallel support on)
from devtime import config, optimize, paths, setup, slotwatch  # noqa: E402

UE = "D:/UE/Engine/Binaries/Win64/UnrealEditor-Cmd.exe D:/proj/My.uproject -run=Foo"
WAIT_LOOP = 'until [ -z "$(tasklist | grep -i UnrealEditor-Cmd)" ]; do sleep 30; done'


def load_hook():
    spec = importlib.util.spec_from_file_location("agent_budget_parallel", os.path.join(ROOT, "hooks", "agent_budget.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(isolate.set_parallel)  # back to the test home's default: parallel support on


class Resolution(Base):
    def test_default_is_off(self):
        isolate.set_parallel(None)
        self.assertFalse(config.parallel())
        self.assertFalse(config.feature("slots"))
        self.assertFalse(config.feature("agent_budget"))

    def test_parallel_turns_both_on(self):
        isolate.set_parallel("parallel = true\n")
        self.assertTrue(config.parallel())
        self.assertTrue(config.feature("slots"))
        self.assertTrue(config.feature("agent_budget"))

    def test_features_override(self):
        isolate.set_parallel("parallel = true\n[settings.features]\nslots = false\n")
        self.assertFalse(config.feature("slots"))
        self.assertTrue(config.feature("agent_budget"))
        isolate.set_parallel("parallel = false\n[settings.features]\nagent_budget = true\n")
        self.assertFalse(config.feature("slots"))
        self.assertTrue(config.feature("agent_budget"))

    def test_bad_file_means_off(self):
        isolate.set_parallel("parallel = [\n")
        self.assertFalse(config.feature("slots"))

    def test_cache_follows_the_file(self):
        isolate.set_parallel("parallel = false\n")
        self.assertFalse(config.feature("slots"))
        with open(isolate.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("[settings]\nparallel = true  # changed\n")
        os.utime(isolate.CONFIG_FILE, ns=(1, 2_000_000_000))  # a different stamp even on a coarse clock
        self.assertTrue(config.feature("slots"))


class HookGating(Base):
    def setUp(self):
        super().setUp()
        self.hook = load_hook()
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = mock.patch.object(self.hook, "STATE_DIR", self.state.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, cmd, agent=None, **extra):
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s-1", "cwd": self.state.name,
                "tool_input": dict({"command": cmd}, **extra)}
        if agent:
            data["agent_id"] = agent
        return self.hook.decide(data)

    def test_unreal_wrapped_only_in_parallel(self):
        out = self.call(UE)
        self.assertIn("slot run", out["hookSpecificOutput"]["updatedInput"]["command"])
        isolate.set_parallel(None)
        self.assertIsNone(self.call(UE))
        isolate.set_parallel("[settings.features]\nslots = true\n")
        self.assertIsNotNone(self.call(UE))

    def test_hand_rolled_wait_denied_only_with_slots(self):
        self.assertEqual(self.call(WAIT_LOOP)["hookSpecificOutput"]["permissionDecision"], "deny")
        isolate.set_parallel(None)
        self.assertIsNone(self.call(WAIT_LOOP))

    def test_subagent_sleep_denied_only_with_agent_budget(self):
        out = self.call("sleep 600", agent="agent-x")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        isolate.set_parallel(None)
        self.assertIsNone(self.call("sleep 600", agent="agent-x"))
        isolate.set_parallel("[settings.features]\nagent_budget = true\n")
        self.assertIsNotNone(self.call("sleep 600", agent="agent-x"))

    def test_alerts_still_delivered_without_parallel(self):
        isolate.set_parallel(None)
        alert = {"key": "k1", "kind": "agent cost", "session": "s-1", "agent": "agent-x", "last": time.time(),
                 "text": "has cost $50", "advice": "stop it", "owner": "x", "model": "m"}
        with mock.patch.object(self.hook, "pending_alerts", side_effect=lambda s, a, st: [alert]):
            out = self.call("sleep 600", agent="agent-x")
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("devtime usage alert", out["hookSpecificOutput"]["additionalContext"])

    def test_ab_enforced_without_parallel(self):
        isolate.set_parallel(None)
        os.makedirs(paths.USER_CONFIG, exist_ok=True)
        path = os.path.join(paths.USER_CONFIG, "experiments.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('[[experiment]]\nid = "e1"\n[[experiment.arms]]\nname = "a"\nmodel = "opus"\n'
                    '[[experiment.arms]]\nname = "b"\nmodel = "opus"\n')
        self.addCleanup(os.remove, path)
        data = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s-1",
                "tool_input": {"description": "AB:e1:s1 do it", "model": "haiku"}}
        out = self.hook.decide(data)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class Watcher(Base):
    def test_does_not_learn_without_parallel(self):
        isolate.set_parallel(None)
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        stop.wait = mock.Mock()
        with mock.patch.object(slotwatch, "take_lock") as lock:
            slotwatch.run_forever(stop=stop)
        lock.assert_not_called()


class SetupParallel(Base):
    def run_cmd(self, value=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.parallel_cmd(value)
        return json.loads(out.getvalue())

    def test_set_and_show(self):
        isolate.set_parallel(None)
        self.assertEqual(self.run_cmd(), dict(parallel=False, features=dict(slots=False, agent_budget=False),
                                              file=isolate.CONFIG_FILE))
        self.assertTrue(self.run_cmd("on")["features"]["slots"])
        with open(isolate.CONFIG_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[settings]\nparallel = true\n")
        self.assertFalse(self.run_cmd("off")["parallel"])
        self.assertFalse(self.run_cmd()["features"]["agent_budget"])

    def test_keeps_the_rest_of_the_file(self):
        text = '# mine\r\n[settings]\r\nport = 9000\r\nparallel = false\r\n[settings.features]\r\nslots = false\r\n'
        with open(isolate.CONFIG_FILE, "wb") as f:
            f.write(text.encode())
        config._USER_SETTINGS.clear()
        self.run_cmd("on")
        with open(isolate.CONFIG_FILE, "rb") as f:
            self.assertEqual(f.read().decode(), text.replace("parallel = false", "parallel = true"))
        text = "[pricing]\nx = 1\n"
        self.assertEqual(setup.set_parallel_text(text, True), text + "\n[settings]\nparallel = true\n")
        self.assertEqual(setup.set_parallel_text("[settings]\nport = 1\n", True),
                         "[settings]\nparallel = true\nport = 1\n")

    def test_check_reports_it(self):
        isolate.set_parallel(None)
        with contextlib.redirect_stdout(io.StringIO()):
            result = setup.check(projects=False)
        self.assertFalse(result["parallel"])
        self.assertEqual(result["features"], {"slots": False, "agent_budget": False})
        self.assertIn("3.11", result["pythonRequired"])


class Catalogue(Base):
    PARALLEL_ONLY = {"handoff-250k", "parallelism", "weekly-cap"}

    def test_requires_parallel(self):
        items = {i["id"]: i for i in optimize.load_catalogue()}
        self.assertEqual({k for k, i in items.items() if i.get("requires") == "parallel"}, self.PARALLEL_ONLY)
        isolate.set_parallel(None)
        self.assertFalse(any(optimize.offered(items[k]) for k in self.PARALLEL_ONLY))
        self.assertTrue(optimize.offered(items["autocompact-400k"]))
        self.assertTrue(optimize.offered(items["slim-agents"]))
        isolate.set_parallel("parallel = true\n")
        self.assertTrue(all(optimize.offered(items[k]) for k in self.PARALLEL_ONLY))

    def test_list_skips_them_until_installed(self):
        isolate.set_parallel(None)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                optimize.list_cmd()
            text = out.getvalue()
            for gone in self.PARALLEL_ONLY:
                self.assertNotIn(gone + " ", text)
            self.assertIn("autocompact-400k", text)
            with contextlib.redirect_stdout(io.StringIO()):
                optimize.apply_cmd("parallelism")  # explicit ids always work
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                optimize.list_cmd()
            self.assertIn("parallelism ", out.getvalue())


if __name__ == "__main__":
    unittest.main()
