"""Organisations: one devtime instance per org. Per-transcript org resolution, the instance's dataset holding only its
org, API responses and hooks of one org only, per-org plan readings, the current-org order, migration, port ownership.

Everything runs on temp dirs (data dir, fake ~/.claude/projects, fake desktop app folder); the real ones are never read.

    python -m unittest discover -s tests
"""

import datetime
import functools
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import alerts, orgs, overview, paths, ports, server, tick  # noqa: E402
from devtime.store import Store  # noqa: E402

ORG_A = "aaaa1111-0000-4000-8000-000000000001"
ORG_B = "bbbb2222-0000-4000-8000-000000000002"
NOW = time.time()


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def usage_result(five, week):
    reset = iso(NOW + 3600)
    return json.dumps({"plan": {"status": "ok", "windows": [
        {"label": "5-hour", "percentUsed": five, "resetsAt": reset},
        {"label": "Weekly (all models)", "percentUsed": week, "resetsAt": iso(NOW + 86400)}]}})


def transcript(path, session, org=None, title=None, usage=None, ago=300, agent=None):
    """A small transcript: [credential_org], [title], a user prompt, one Bash call with its result, a closing reply,
    and optionally a get_usage call with the given (five, week) reading."""
    t = NOW - ago
    base = {"sessionId": session, "cwd": "/work/" + session, "gitBranch": "main", "version": "2.0"}
    if agent:
        base["agentId"] = agent
    rows = []
    if org:
        rows.append(dict(base, type="attachment", timestamp=iso(t),
                         attachment={"type": "credential_org", "organizationUuid": org}))
    if title:
        rows.append({"type": "custom-title", "customTitle": title, "sessionId": session})
    rows.append(dict(base, type="user", timestamp=iso(t + 1), message={"role": "user", "content": "hello"}))

    def reply(n, content):
        return dict(base, type="assistant", timestamp=iso(t + n), requestId="r%s%d" % (session, n),
                    message={"id": "m%s%d" % (session, n), "model": "claude-sonnet-4-5", "content": content,
                             "usage": {"input_tokens": 1000, "output_tokens": 1000}})

    rows.append(reply(5, [{"type": "tool_use", "id": "tu%s" % session, "name": "Bash",
                           "input": {"command": "echo hi", "description": "say hi"}}]))
    rows.append(dict(base, type="user", timestamp=iso(t + 6), message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu%s" % session, "content": "hi"}]}))
    if usage:
        rows.append(reply(8, [{"type": "tool_use", "id": "gu%s" % session,
                               "name": "mcp__ccd_session_mgmt__get_usage", "input": {}}]))
        rows.append(dict(base, type="user", timestamp=iso(t + 9), message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "gu%s" % session,
             "content": [{"type": "text", "text": usage_result(*usage)}]}]}))
    rows.append(reply(12, [{"type": "text", "text": "done"}]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")


def subagent(folder, session, agent, description, org=None, ago=200):
    path = os.path.join(folder, session, "subagents", "agent-%s.jsonl" % agent)
    transcript(path, session, org, ago=ago, agent=agent)
    with open(path[:-6] + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"agentType": "general-purpose", "description": description}, f)


def patch_default(test, fn, index, value):
    """Replaces default argument `index` of fn for the test (module-level default paths were bound at import)."""
    defaults = list(fn.__defaults__)
    defaults[index] = value
    patcher = mock.patch.object(fn, "__defaults__", tuple(defaults))
    patcher.start()
    test.addCleanup(patcher.stop)


class Fixture(unittest.TestCase):
    """An org instance (`org`, default A) on a machine whose transcripts belong to A, B and nobody known."""
    org = ORG_A
    include_unknown = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.root = root
        self.data_dir = os.path.join(root, "data")
        self.projects = os.path.join(root, "projects")
        self.desktop = os.path.join(root, "Claude")
        os.makedirs(os.path.join(self.data_dir, "config"))
        self.instance = os.path.join(self.data_dir, "orgs", self.org)
        patcher = mock.patch.multiple(paths, DATA=self.data_dir, INSTANCE=self.instance, ORG=self.org,
                                      CACHE=os.path.join(self.instance, "cache"), LOGS=os.path.join(self.data_dir, "logs"),
                                      USER_CONFIG=os.path.join(self.data_dir, "config"),
                                      LEGACY_CACHE=os.path.join(self.data_dir, "cache"))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"DEVTIME_DESKTOP_DIR": self.desktop, "DEVTIME_HOME": self.data_dir,
                                           "CLAUDE_CONFIG_DIR": root, "DEVTIME_NO_SERVER_START": "1",
                                           paths.ORG_ENV: self.org})
        env.start()
        self.addCleanup(env.stop)
        self.cache = paths.CACHE
        self.manual = os.path.join(self.cache, "plan_samples.jsonl")
        patch_default(self, overview.plan_samples, 0, self.manual)
        patch_default(self, overview.changes, 0, os.path.join(self.data_dir, "config", "changes.toml"))
        patch_default(self, overview.work_sections, 0, os.path.join(self.cache, "work_sections.json"))
        patch_default(self, overview.assign_work, 0, os.path.join(self.cache, "work_sections.json"))
        patch_default(self, overview.record_plan_sample, 0, self.manual)
        patch_default(self, alerts.notes, 0, os.path.join(self.cache, "review_notes.jsonl"))
        patch_default(self, alerts.record_note, 0, os.path.join(self.cache, "review_notes.jsonl"))
        overview._harvest_mem["cache"] = None
        self.harvest_cache = os.path.join(self.cache, "usage_harvest.json")
        harvest = functools.partial(overview.harvest_usage, root=self.projects, cache=self.harvest_cache)
        patcher = mock.patch.object(overview, "harvest_usage", harvest)
        patcher.start()
        self.addCleanup(patcher.stop)

        pa, pb = os.path.join(self.projects, "projA"), os.path.join(self.projects, "projB")
        # tagged A, with an untagged subagent (parent's org) and a get_usage reading
        transcript(os.path.join(pa, "sA.jsonl"), "sA", ORG_A, "Alpha main", usage=(11, 41), ago=900)
        subagent(pa, "sA", "x1", "ALPHA-task", ago=800)
        # tagged B (the most recent), subagent untagged, with its own reading
        transcript(os.path.join(pb, "sB.jsonl"), "sB", ORG_B, "Bravo main", usage=(22, 52), ago=100)
        subagent(pb, "sB", "y1", "BRAVO-task", ago=60)
        # no tag: the desktop index says B; its subagent follows
        transcript(os.path.join(pb, "sD.jsonl"), "sD", None, "Delta main", ago=500)
        subagent(pb, "sD", "d1", "BRAVO-desktop-task", ago=400)
        # tagged A although the desktop index says B: the tag wins
        transcript(os.path.join(pa, "sT.jsonl"), "sT", ORG_A, "Tag wins", ago=700)
        # a subagent with its own tag
        subagent(pa, "sT", "t1", "ALPHA-own-tag", org=ORG_A, ago=600)
        # no evidence at all
        transcript(os.path.join(pa, "sU.jsonl"), "sU", None, "Unknown main", usage=(33, 63), ago=1500)
        self.write_desktop("claude-code-sessions", "acct", ORG_B, "local_1", "sD")
        self.write_desktop("claude-code-sessions", "acct", ORG_B, "local_2", "sT")
        self.write_desktop("local-agent-mode-sessions", "acct", "skills-plugin", "local_3", "sU")
        self.write_config()
        self.store = Store(verbose=False)
        self.store.refresh()
        self.by_id = {t["id"]: t for t in self.store.data["transcripts"]}

    def write_desktop(self, kind, account, org, name, session):
        folder = os.path.join(self.desktop, kind, account, org)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as f:
            json.dump({"cliSessionId": session, "cwd": "/x"}, f)

    def write_config(self, extra=""):
        self.history = os.path.join(self.root, "plan-usage-history.json")
        text = ('[settings]\nprojects_dir = %s\nextra_project_dirs = []\ndesktop_session_dirs = []\n'
                'plan_usage_history = %s\ninclude_unknown_org = %s\n\n[settings.alerts]\nagent_cost = 0.000001\n'
                'machine_burn = 0.000001\n' % (json.dumps(self.projects), json.dumps(self.history),
                                               "true" if self.include_unknown else "false"))
        with open(os.path.join(self.data_dir, "config", "devtime.toml"), "w", encoding="utf-8") as f:
            f.write(text + extra)


A_IDS = {"sA", "x1", "sT", "t1"}
B_IDS = {"sB", "y1", "sD", "d1"}


class Resolution(Fixture):
    def test_classify_order_tag_desktop_parent_unknown(self):
        from devtime import scan
        discovered = scan.discover(self.projects)
        found = orgs.classify(discovered, {}, orgs.desktop_index())
        org = {d.get("agent") or d["session"]: found[d["path"]] for d in discovered}
        self.assertEqual(org["sA"], ORG_A)          # own tag
        self.assertEqual(org["sT"], ORG_A)          # own tag beats the desktop index (which says B)
        self.assertEqual(org["sD"], ORG_B)          # no tag: desktop index
        self.assertEqual(org["x1"], ORG_A)          # subagent: its parent's
        self.assertEqual(org["d1"], ORG_B)          # subagent of a desktop-resolved session
        self.assertEqual(org["t1"], ORG_A)          # subagent with its own tag
        self.assertEqual(org["sU"], orgs.UNKNOWN)   # skills-plugin folder is skipped; no evidence

    def test_dataset_is_the_instance_org_only(self):
        self.assertEqual(set(self.by_id), A_IDS)
        self.assertEqual({t["org"] for t in self.by_id.values()}, {ORG_A})
        self.assertEqual(self.store.data["org"], ORG_A)
        self.assertFalse(self.store.data["singleOrg"])

    def test_other_orgs_transcripts_are_not_scanned_or_cached(self):
        cached = {os.path.basename(p)[:-6] for p in self.store.cache.states}
        self.assertEqual(cached, {"sA", "agent-x1", "sT", "agent-t1"})
        self.store.cache.save()
        with open(self.store.cache.path, "rb") as f:
            raw = f.read()
        for word in (b"BRAVO", b"Bravo", b"Delta", b"Unknown main"):
            self.assertNotIn(word, raw)
        tags = {os.path.basename(p): t for p, (_, t) in self.store.cache.tags.items()}
        self.assertEqual(tags["sB.jsonl"], ORG_B)  # of another org's file only the tag is known
        self.assertIsNone(tags["sD.jsonl"])

    def test_cache_version_bumped(self):
        from devtime import scan
        self.assertGreaterEqual(scan.CACHE_VERSION, 8)

    def test_a_late_tag_is_picked_up(self):
        path = os.path.join(self.projects, "projA", "sU.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "attachment", "attachment": {"type": "credential_org",
                                                                      "organizationUuid": ORG_A}}) + "\n")
        self.store.refresh()
        self.assertIn("sU", {t["id"] for t in self.store.data["transcripts"]})


class InstanceB(Fixture):
    org = ORG_B

    def test_dataset_is_b_only(self):
        self.assertEqual(set(self.by_id), B_IDS)
        body = json.dumps(self.store.slice("*"))
        for word in ("ALPHA", "Alpha", ORG_A):
            self.assertNotIn(word, body)


class UnknownIncluded(Fixture):
    include_unknown = True

    def test_untagged_transcripts_are_included_when_asked(self):
        self.assertEqual(set(self.by_id), A_IDS | {"sU"})
        self.assertNotIn("sB", self.by_id)
        weeks = [round(s[2], 2) for s in overview.plan_samples(self.store.config.settings, ORG_A)]
        self.assertEqual(sorted(weeks), [0.41, 0.63])


class Readings(Fixture):
    def test_harvested_readings_are_the_instance_orgs_only(self):
        settings = self.store.config.settings
        a = overview.plan_samples(settings, ORG_A)
        self.assertEqual([(round(s[1], 2), round(s[2], 2), s[5]) for s in a], [(0.11, 0.41, ORG_A)])
        self.assertEqual(overview.plan_samples(settings, None), [])
        with open(self.harvest_cache, encoding="utf-8") as f:
            entries = {os.path.basename(p): e for p, e in json.load(f)["files"].items()}
        self.assertTrue(entries["sB.jsonl"]["skip"])  # read no further than its org tag
        self.assertEqual(entries["sB.jsonl"]["readings"], [])
        self.assertEqual(entries["sU.jsonl"]["readings"], [])

    def test_manual_samples_belong_to_the_instance_org(self):
        settings = self.store.config.settings
        overview.record_plan_sample(7, None, 70, None)
        with open(self.manual, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": NOW, "fh": 1, "sd": 80, "org": "unknown"}) + "\n")
            f.write(json.dumps({"t": NOW, "fh": 1, "sd": 90, "org": ORG_B}) + "\n")
        self.assertEqual(sorted(round(s[2], 2) for s in overview.plan_samples(settings, ORG_A)), [0.41, 0.7])

    def test_desktop_history_by_org_and_untagged_only_on_a_single_org_machine(self):
        settings = self.store.config.settings
        with open(self.history, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "samples": [
                {"t": (NOW - 5000) * 1000, "org": ORG_A, "u": {"fh": 1, "sd": 10}},
                {"t": (NOW - 4000) * 1000, "org": ORG_B, "u": {"fh": 1, "sd": 20}},
                {"t": (NOW - 3000) * 1000, "u": {"fh": 1, "sd": 30}}]}, f)
        weeks = lambda **kw: sorted(round(s[2], 2) for s in overview.plan_samples(settings, ORG_A, **kw))
        self.assertEqual(weeks(), [0.1, 0.41])
        self.assertEqual(weeks(single_org=True), [0.1, 0.3, 0.41])

    def test_tick_reading_and_limits(self):
        out = overview.tick_summary(self.store.slice("*"), self.store.config.settings, [], NOW)
        self.assertEqual(out["reading"]["weekly"], 41.0)
        data = self.store.slice("*")
        settings = dict(self.store.config.settings, weekly_budget_usd=5000)  # the fixture has too little cost to fit one
        plan = overview.limits(data, overview.Index(data), settings, NOW)
        self.assertEqual(round(plan["reading"]["share"], 2), 0.41)


def get(base, path, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=30) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


class Scoping(Fixture):
    def setUp(self):
        super().setUp()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.store))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def text(self, path):
        status, body = get(self.base, path)
        self.assertEqual(status, 200, (path, body[:200]))
        return body

    THEIRS = ("BRAVO", "Bravo", "Delta", ORG_B, "bbbb2222", "sB", "sD", '"y1"', '"d1"', "Unknown main")
    PATHS = ("/api/overview?range=7d", "/api/watch?hours=6", "/api/alerts", "/api/projects", "/api/tick",
             "/api/data", "/api/data?project=projB")

    def test_version_has_home_and_org(self):
        v = json.loads(self.text("/api/version"))
        self.assertEqual((v["home"], v["org"]), (paths.home_id(), ORG_A))

    def test_nothing_of_another_org_anywhere(self):
        for path in self.PATHS:
            body = self.text(path)
            for word in self.THEIRS:
                self.assertNotIn(word, body, (path, word))
        self.assert_in(self.text("/api/watch?hours=6"), "Alpha main", "ALPHA-task")
        self.assert_in(self.text("/api/alerts"), "ALPHA-task")
        self.assertEqual(json.loads(self.text("/api/tick"))["org"], ORG_A)
        data = json.loads(self.text("/api/data"))
        self.assertEqual({t["org"] for t in data["transcripts"]}, {ORG_A})
        self.assertEqual(sum(p["sessions"] for p in data["projects"]), 2)

    def assert_in(self, body, *words):
        for word in words:
            self.assertIn(word, body)

    def test_no_org_selection_left(self):
        self.assertEqual(get(self.base, "/api/orgs")[0], 404)
        body = self.text("/api/data?org=" + ORG_A)
        self.assertNotIn("BRAVO", body)

    def test_callers_that_say_who_they_are_are_checked(self):
        self.text("/api/overview?session=sA&org=" + ORG_A)
        for query in ("session=sB", "session=sD", "session=sU", "session=nope", "org=" + ORG_B,
                      "session=sA&org=" + ORG_B):
            for path in ("/api/overview", "/api/alerts"):
                status, body = get(self.base, path + "?" + query)
                self.assertEqual(status, 403, (path, query))
                self.assertNotIn("ALPHA", body)

    def test_alerts_and_machine_burn_are_the_instance_orgs(self):
        items = json.loads(self.text("/api/alerts"))["alerts"]
        self.assertTrue(items)
        self.assertEqual({a["org"] for a in items}, {ORG_A})
        self.assertEqual(len([a for a in items if a["kind"] == "machine burn"]), 1)

    def test_work_items_are_the_instance_orgs(self):
        body = json.dumps(overview.work_items(self.store.slice("*"), NOW, 8, 100, assign={}))
        self.assertIn("ALPHA-task", body)
        self.assertNotIn("BRAVO", body)

    def test_local_tick_numbers(self):
        with mock.patch.object(tick, "local_store", return_value=self.store):
            out = tick.tick_numbers("http://x/", False, NOW)
        self.assertEqual(out["reading"]["weekly"], 41.0)

    def test_no_org_no_data(self):
        with mock.patch.object(paths, "ORG", None):
            self.assertEqual(get(self.base, "/api/overview")[0], 403)
            self.assertIsNone(json.loads(self.text("/api/version"))["org"])


class Review(Fixture):
    def test_notes_reach_only_the_instance_orgs_sessions(self):
        alerts.record_note("Alpha main", "check models")  # recorded for this instance's org
        alerts.record_note("Tag wins", "from elsewhere", org=ORG_B)
        with open(os.path.join(self.cache, "review_notes.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "session": "Tag wins", "text": "no org"}) + "\n")
        alerts.record_note("Bravo main", "check waits")  # no such session here
        current = alerts.check(self.store.data, self.store.config, time.time() + 1)
        notes = [a for a in current if a["kind"] == "usage review"]
        self.assertEqual([(a["owner"], a["org"]) for a in notes], [("Alpha main", ORG_A)])


class CurrentOrg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(paths.ORG_ENV, None)

    def login(self, org, name="Work Ltd"):
        with open(os.path.join(self.tmp.name, ".claude.json"), "w", encoding="utf-8") as f:
            json.dump({"oauthAccount": {"organizationUuid": org, "organizationName": name}}, f)

    def test_env_then_claude_json_then_none(self):
        self.assertEqual(paths.current_org(), (None, None))
        self.login(ORG_B)
        self.assertEqual(paths.current_org(), (ORG_B, "claude.json"))
        os.environ[paths.ORG_ENV] = ORG_A
        self.assertEqual(paths.current_org(), (ORG_A, "env"))  # the desktop session's own org wins

    def test_bad_values_are_no_org(self):
        os.environ[paths.ORG_ENV] = "../x"
        self.assertEqual(paths.current_org(), (None, None))  # an env that names no plain id: not even the login
        del os.environ[paths.ORG_ENV]
        self.login("a/b")
        self.assertEqual(paths.current_org(), (None, None))

    def test_name_only_for_the_logins_org(self):
        self.login(ORG_B)
        self.assertEqual(paths.org_name(ORG_B), "Work Ltd")
        self.assertEqual(paths.org_name(ORG_A), "aaaa1111")

    def test_allowed_orgs(self):
        with mock.patch.object(paths, "ORG", ORG_A):
            self.assertEqual(paths.allowed_orgs({}), {ORG_A})
            self.assertEqual(paths.allowed_orgs({"include_unknown_org": True}), {ORG_A, "unknown"})
        with mock.patch.object(paths, "ORG", None):
            self.assertEqual(paths.allowed_orgs({"include_unknown_org": True}), set())


def load_hook(name):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("hook_" + name, os.path.join(root, "hooks", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Hooks(unittest.TestCase):
    """The hooks only touch the instance of the session's org, and nothing when it is unknown."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hook = load_hook("agent_budget")
        for org, text in ((ORG_A, "alpha alert"), (ORG_B, "bravo alert")):
            folder = os.path.join(self.tmp.name, org)
            os.makedirs(folder)
            with open(os.path.join(folder, "alerts.json"), "w", encoding="utf-8") as f:
                json.dump({"alerts": [{"key": org + ":k", "kind": "burn", "session": "s1", "agent": None,
                                       "last": time.time(), "owner": "x", "model": "m", "text": text,
                                       "advice": "a"}]}, f)

    def deliver(self, org):
        folder = os.path.join(self.tmp.name, org or "none")
        with mock.patch.object(paths, "ORG", org), mock.patch.object(paths, "CACHE", folder), \
                mock.patch.object(self.hook, "ALERTS", os.path.join(folder, "alerts.json")), \
                mock.patch.object(self.hook, "STATE_DIR", os.path.join(folder, "agent_budget")):
            out = self.hook.decide({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                                    "tool_input": {"command": "ls"}})
        return json.dumps(out) if out else ""

    def test_alerts_of_the_sessions_own_org_only(self):
        mine = self.deliver(ORG_A)
        self.assertIn("alpha alert", mine)
        self.assertNotIn("bravo", mine)
        theirs = self.deliver(ORG_B)
        self.assertIn("bravo alert", theirs)
        self.assertNotIn("alpha", theirs)

    def test_nothing_without_an_org(self):
        os.makedirs(os.path.join(self.tmp.name, "none"))
        with open(os.path.join(self.tmp.name, "none", "alerts.json"), "w", encoding="utf-8") as f:
            json.dump({"alerts": [{"key": "k", "kind": "burn", "session": "s1", "agent": None, "last": time.time(),
                                   "owner": "x", "model": "m", "text": "leak", "advice": "a"}]}, f)
        self.assertEqual(self.deliver(None), "")

    def test_ensure_server_starts_nothing_without_an_org(self):
        hook = load_hook("ensure_server")
        with mock.patch.object(paths, "ORG", None), mock.patch.object(hook, "port") as port, \
                mock.patch.object(hook.subprocess, "Popen") as popen, \
                mock.patch.object(paths, "install_launcher"):
            hook.main()
        port.assert_not_called()
        popen.assert_not_called()


class Migration(Fixture):
    """The pre-0.4.0 shared cache is split by org; nothing of one org lands in another's instance."""

    def legacy(self, name, rows=None, text=None):
        os.makedirs(paths.LEGACY_CACHE, exist_ok=True)
        with open(os.path.join(paths.LEGACY_CACHE, name), "w", encoding="utf-8") as f:
            f.write(text if text is not None else "".join(json.dumps(r) + "\n" for r in rows))

    def read(self, org, name):
        try:
            with open(os.path.join(paths.instance_of(org), "cache", name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def fill(self):
        self.legacy("plan_samples.jsonl", [{"t": 1, "fh": 1, "sd": 10, "org": ORG_A},
                                           {"t": 2, "fh": 1, "sd": 20, "org": ORG_B},
                                           {"t": 3, "fh": 1, "sd": 30, "org": "aaaa1111"},
                                           {"t": 4, "fh": 1, "sd": 40, "org": "unknown"}])
        self.legacy("review_notes.jsonl", [{"t": 1, "session": "Alpha main", "text": "ALPHA-NOTE", "org": ORG_A},
                                           {"t": 2, "session": "Bravo main", "text": "BRAVO-NOTE", "org": ORG_B},
                                           {"t": 3, "session": "Who", "text": "NO-ORG-NOTE"}])
        self.legacy("work_sections.json", text=json.dumps({"assign": {"x1": "Alpha work", "y1": "Bravo work",
                                                                      "gone": "Old work"}}))
        self.legacy("review-log.md", text="2026-10-16 10:00 | Bravo main: use haiku\n")
        self.legacy("transcripts.pickle", text="old")
        self.legacy("alerts.json", text='{"alerts": []}')

    def test_split_by_org(self):
        from devtime import migrate
        self.fill()
        written = migrate.run(self.store.config.settings)
        self.assertEqual(set(written), {ORG_A, ORG_B})
        a, b = self.read(ORG_A, "plan_samples.jsonl"), self.read(ORG_B, "plan_samples.jsonl")
        self.assertEqual(sorted(json.loads(x)["sd"] for x in a.splitlines()), [10, 30])
        self.assertEqual([json.loads(x)["sd"] for x in b.splitlines()], [20])
        self.assertIn("ALPHA-NOTE", self.read(ORG_A, "review_notes.jsonl"))
        for org in (ORG_A, ORG_B):
            notes = self.read(org, "review_notes.jsonl")
            self.assertNotIn("NO-ORG-NOTE", notes)
        self.assertNotIn("BRAVO", self.read(ORG_A, "review_notes.jsonl") + self.read(ORG_A, "work_sections.json"))
        self.assertEqual(json.loads(self.read(ORG_A, "work_sections.json"))["assign"], {"x1": "Alpha work"})
        self.assertEqual(json.loads(self.read(ORG_B, "work_sections.json"))["assign"], {"y1": "Bravo work"})
        self.assertEqual(self.read(ORG_A, "review-log.md") + self.read(ORG_B, "review-log.md"), "")  # whose?
        self.assertFalse(os.path.exists(paths.LEGACY_CACHE))
        self.assertIsNone(migrate.run(self.store.config.settings))

    def test_rerun_does_not_duplicate(self):
        from devtime import migrate
        self.fill()
        migrate.run(self.store.config.settings)
        self.fill()
        migrate.run(self.store.config.settings)
        self.assertEqual(len(self.read(ORG_A, "plan_samples.jsonl").splitlines()), 2)


class SingleOrgMigration(Migration):
    """Only org A on the machine: rows without an org and the review log are A's."""

    def setUp(self):
        super().setUp()
        import shutil
        shutil.rmtree(os.path.join(self.projects, "projB"))
        shutil.rmtree(os.path.join(self.desktop, "claude-code-sessions"))
        os.remove(os.path.join(self.projects, "projA", "sU.jsonl"))

    def test_split_by_org(self):
        from devtime import migrate
        self.fill()
        written = migrate.run(self.store.config.settings)
        self.assertIn(ORG_A, written)
        a = self.read(ORG_A, "plan_samples.jsonl")
        self.assertEqual(sorted(json.loads(x)["sd"] for x in a.splitlines()), [10, 30, 40])
        self.assertIn("NO-ORG-NOTE", self.read(ORG_A, "review_notes.jsonl"))
        self.assertIn("use haiku", self.read(ORG_A, "review-log.md"))
        self.assertEqual(json.loads(self.read(ORG_A, "work_sections.json"))["assign"],  # no other org known
                         {"x1": "Alpha work", "y1": "Bravo work", "gone": "Old work"})
        self.assertEqual([json.loads(x)["sd"] for x in self.read(ORG_B, "plan_samples.jsonl").splitlines()], [20])
        self.store.refresh(force=True)
        self.assertTrue(self.store.data["singleOrg"])

    def test_rerun_does_not_duplicate(self):
        from devtime import migrate
        self.fill()
        migrate.run(self.store.config.settings)
        self.fill()
        migrate.run(self.store.config.settings)
        self.assertEqual(len(self.read(ORG_A, "plan_samples.jsonl").splitlines()), 3)


class FakeServer:
    """Answers /api/version with the given home on a free port."""

    def __init__(self, home):
        body = json.dumps({"version": 1, "home": home}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class PortOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(data, "config"))
        instance = os.path.join(data, "orgs", ORG_A)
        patcher = mock.patch.multiple(paths, DATA=data, INSTANCE=instance, CACHE=os.path.join(instance, "cache"),
                                      USER_CONFIG=os.path.join(data, "config"), ORG=ORG_A)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"DEVTIME_NO_SERVER_START": "1", "DEVTIME_HOME": data})
        env.start()
        self.addCleanup(env.stop)

    def configure(self, port):
        with open(os.path.join(paths.USER_CONFIG, "devtime.toml"), "w", encoding="utf-8") as f:
            f.write("[settings]\nport = %d\n" % port)

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def fake(self, home):
        server_ = FakeServer(home)
        self.addCleanup(server_.close)
        return server_

    def test_home_id_depends_on_the_instance(self):
        first = paths.home_id()
        self.assertRegex(first, "^[0-9a-f]{12}$")
        with mock.patch.object(paths, "INSTANCE", paths.instance_of(ORG_B)):  # another org's server is not ours
            self.assertNotEqual(paths.home_id(), first)

    def test_other_homes_server_moves_us_to_next_free_port(self):
        other = self.fake("0123456789ab")
        self.configure(other.port)
        chosen = ports.choose()
        self.assertTrue(other.port < chosen <= other.port + ports.SPAN, (other.port, chosen))
        self.assertFalse(ports.answering(chosen))
        with open(ports.port_file(), encoding="utf-8") as f:
            self.assertEqual(int(f.read()), chosen)
        self.assertFalse(ports.ours(other.port))

    def test_own_server_keeps_the_port(self):
        mine = self.fake(paths.home_id())
        self.configure(mine.port)
        self.assertEqual(ports.choose(), mine.port)
        self.assertTrue(ports.ours(mine.port))

    def test_free_configured_port_is_kept(self):
        free = self.free_port()
        self.configure(free)
        self.assertEqual(ports.choose(), free)

    def test_recorded_own_port_is_found_again(self):
        other = self.fake("someone-else")
        mine = self.fake(paths.home_id())
        self.configure(other.port)
        ports.record(mine.port)
        self.assertEqual(ports.choose(), mine.port)

    def test_setup_ensure_server_reads_the_port_file(self):
        from devtime import setup
        mine = self.fake(paths.home_id())
        self.configure(mine.port)
        url, up = setup.ensure_server(wait_s=0)
        self.assertEqual((url, up), ("http://127.0.0.1:%d/" % mine.port, True))
        mine.close()
        os.remove(ports.port_file())
        other = self.fake("someone-else")
        self.configure(other.port)
        url, up = setup.ensure_server(wait_s=0)  # not ours: DEVTIME_NO_SERVER_START keeps it from starting one
        self.assertFalse(up)
        self.assertNotEqual(url, "http://127.0.0.1:%d/" % other.port)


if __name__ == "__main__":
    unittest.main()
