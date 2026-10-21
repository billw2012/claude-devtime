"""devtime's own Claude usage (devtime.own): subagents of devtime agent types and main-session turns driven by devtime
skills, matched by name only.

    python -m unittest discover -s tests
"""

import contextlib
import datetime
import io
import json
import os
import pickle
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import analyze, config, overview, own, report, scan  # noqa: E402

SECRET = "SECRETPROMPTWORDS"
T0 = datetime.datetime(2026, 10, 12, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
MINE = 6.5 + 128 + 256 + 32 + 2048  # loop + labeller + reviewer + setup (skill turn, devtime:future agent)


def stamp(n):
    return datetime.datetime.fromtimestamp(T0 + 10 * n, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Writer:
    """Builds one transcript; a request of cost c has c * 40000 output tokens (claude-opus-5: $25 per million)."""

    def __init__(self, path, session):
        self.path, self.session, self.n = path, session, 0
        self.tag = os.path.basename(path)[:-6]  # request ids must differ between transcripts
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def add(self, record):
        self.n += 1
        record.update(timestamp=stamp(self.n), sessionId=self.session, cwd="C:/proj")
        with open(self.path, "a", encoding="utf8") as handle:
            handle.write(json.dumps(record) + "\n")

    def user(self, content, **extra):
        self.add(dict(type="user", message={"role": "user", "content": content}, **extra))

    def ask(self, cost, tool=None):
        content = [{"type": "text", "text": SECRET}]
        if tool:
            content.append({"type": "tool_use", "id": "tu%s%d" % (self.tag, self.n + 1), "name": tool[0], "input": tool[1]})
        self.add({"type": "assistant", "requestId": "r%s%d" % (self.tag, self.n + 1), "message": {
            "id": "m%s%d" % (self.tag, self.n + 1), "model": "claude-opus-5", "role": "assistant", "content": content,
            "usage": {"input_tokens": 0, "output_tokens": int(cost * 40000)}}})
        return "tu%s%d" % (self.tag, self.n)

    def result(self, tool_id):
        self.user([{"type": "tool_result", "tool_use_id": tool_id, "content": SECRET}])


class Own(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        proj = os.path.join(self.tmp.name, "projects", "proj")
        main = Writer(os.path.join(proj, "s1.jsonl"), "s1")
        main.user("plain prompt " + SECRET)
        main.ask(1)
        main.user("<command-message>start</command-message>\n<command-name>/devtime:start</command-name>\n"
                  "<command-args>tick " + SECRET + "</command-args>")
        main.ask(2)
        main.ask(4)
        main.user("please do my task " + SECRET)
        main.ask(8)
        tid = main.ask(16, ("Skill", {"skill": "devtime:optimize", "args": SECRET}))
        main.result(tid)
        main.ask(32)
        main.user("another normal prompt " + SECRET)
        main.ask(64)
        tid = main.ask(0.25, ("Skill", {"skill": "other:thing"}))
        main.result(tid)
        main.ask(0.25)
        main.user("/devtime:start tick")  # sent as plain text (a scheduled prompt)
        main.ask(0.5)
        main.user("last normal prompt " + SECRET)
        main.ask(16384)
        for agent, kind, cost in (("a1", "devtime:labeller", 128), ("a2", "devtime:reviewer", 256),
                                  ("a3", "devtime:coder", 512), ("a4", "devtime:runner", 1024),
                                  ("a5", "devtime:future", 2048), ("a6", "general-purpose", 4096),
                                  ("a7", "devtime:reader", 8192)):
            path = os.path.join(proj, "s1", "subagents", "agent-%s.jsonl" % agent)
            sub = Writer(path, "s1")
            sub.n = 100
            sub.user("do it " + SECRET)
            sub.ask(cost)
            with open(path[:-6] + ".meta.json", "w") as handle:
                json.dump({"agentType": kind, "description": "d " + agent}, handle)
        self.cfg = config.Config()
        self.cache = scan.Cache(os.path.join(self.tmp.name, "cache"))
        discovered = scan.discover(os.path.join(self.tmp.name, "projects"))
        self.cache.refresh({d["path"] for d in discovered})
        self.data = analyze.Builder(self.cfg, self.cache, discovered, {}).build()
        self.idx = overview.Index(self.data)
        self.total = sum(t["cost"] for t in self.data["transcripts"])

    def test_classification(self):
        mine = overview.own_usage(self.data, self.idx, 0, float("inf"), self.total)
        self.assertEqual(mine["byCategory"], {"loop": 6.5, "labeller": 128.0, "reviewer": 256.0,
                                              "setup & optimize": 32.0 + 2048.0})
        self.assertAlmostEqual(mine["cost"], MINE)
        self.assertAlmostEqual(self.total, 1 + 2 + 4 + 8 + 16 + 32 + 64 + 0.5 + 0.5 + 16384 + 128 + 256 + 512 + 1024
                               + 2048 + 4096 + 8192)
        self.assertAlmostEqual(mine["share"], round(MINE / self.total, 4), places=4)
        self.assertEqual(mine["requests"], 2 + 1 + 1 + 1 + 1 + 1)

    def test_not_counted(self):
        # plain turns, the request that issued the Skill call, a user prompt after a devtime turn, a non-devtime
        # Skill; coder / reader / runner / other agents are the user's own work
        iv, tok = self.data["iv"], self.data["tok"]
        flags = {}
        for j, i in enumerate(tok["i"]):
            flags[round(tok["cost"][j], 3)] = (iv["f"][i] // analyze.F_OWN) & 15
        for plain in (1, 8, 16, 64, 0.25, 16384, 512, 1024, 4096, 8192):
            self.assertEqual(flags[plain], 0, plain)
        for counted in (2, 4, 32, 128, 256, 2048, 0.5):
            self.assertNotEqual(flags[counted], 0, counted)

    def test_range(self):
        late = T0 + 10 * 100
        mine = overview.own_usage(self.data, self.idx, late, float("inf"), 1.0)
        self.assertEqual(mine["byCategory"]["labeller"], 128.0)
        self.assertEqual(mine["byCategory"]["loop"], 0.0)
        none = overview.own_usage(self.data, self.idx, T0 + 10 ** 6, float("inf"), None)
        self.assertEqual((none["cost"], none["share"], none["requests"]), (0, None, 0))

    def test_overview_and_tick_fields(self):
        settings = self.cfg.settings
        o = overview.overview(self.data, self.idx, "all", settings, week_cost=0, now=T0 + 3600)
        self.assertEqual(set(o["devtime"]), {"cost", "share", "requests", "byCategory"})
        self.assertEqual(list(o["devtime"]["byCategory"]), list(own.CATEGORIES))
        self.assertAlmostEqual(o["devtime"]["cost"], MINE)
        self.assertAlmostEqual(o["devtime"]["share"], MINE / self.total, places=3)
        t = overview.tick_summary(self.data, settings, [], T0 + 3600)
        self.assertEqual(set(t["devtime"]), {"cost", "share", "requests", "byCategory"})
        self.assertAlmostEqual(t["devtime"]["cost"], MINE)
        self.assertAlmostEqual(t["devtime"]["share"], MINE / self.total, places=3)
        self.assertEqual(t["devtimeDays"], 7)
        self.assertEqual(t["devtime"]["byCategory"]["labeller"], 128.0)

    def test_report_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report.report(self.data)
        line = [x for x in out.getvalue().splitlines() if x.startswith("devtime's own usage")]
        self.assertEqual(len(line), 1)
        self.assertIn("loop $6.50", line[0])
        self.assertIn("labeller $128.00", line[0])
        self.assertIn("% of cost", line[0])

    def test_no_prompt_text_stored(self):
        blob = json.dumps(self.data, default=str) + repr(pickle.dumps(self.cache.states)) \
            + repr(pickle.dumps(self.data))
        self.assertNotIn(SECRET, blob)
        cmds = [e for st in self.cache.states.values() for e in st.events if e[0] == "cmd"]
        self.assertEqual([e[2] for e in cmds], ["devtime:start", "devtime:start"])  # the names only

    def test_names(self):
        self.assertEqual(own.command_name("<command-name>/devtime:optimize</command-name><command-args>x"),
                         "devtime:optimize")
        self.assertEqual(own.command_name("/devtime:start tick"), "devtime:start")
        self.assertIsNone(own.command_name("please run /devtime:start"))
        self.assertIsNone(own.command_name("<command-name>/compact</command-name>"))
        self.assertEqual(own.skill_category("devtime:start"), "loop")
        self.assertEqual(own.skill_category("devtime:ab"), "setup & optimize")
        self.assertIsNone(own.skill_category("other:start"))
        self.assertIsNone(own.skill_category("devtime:"))
        self.assertIsNone(own.agent_category("devtime:coder"))
        self.assertIsNone(own.agent_category("general-purpose"))
        self.assertEqual(own.agent_category("devtime:labeller"), "labeller")


if __name__ == "__main__":
    unittest.main()
