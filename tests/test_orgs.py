"""Organisations: per-transcript org resolution, scoped API responses, per-org plan readings, port ownership.

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
    allow_all = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.data_dir = os.path.join(root, "data")
        self.projects = os.path.join(root, "projects")
        self.desktop = os.path.join(root, "Claude")
        os.makedirs(os.path.join(self.data_dir, "config"))
        for target, values in ((paths, {"DATA": self.data_dir, "CACHE": os.path.join(self.data_dir, "cache"),
                                        "USER_CONFIG": os.path.join(self.data_dir, "config")}),):
            patcher = mock.patch.multiple(target, **values)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"DEVTIME_DESKTOP_DIR": self.desktop, "DEVTIME_HOME": self.data_dir,
                                           "CLAUDE_CONFIG_DIR": root, "DEVTIME_NO_SERVER_START": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.cache = paths.CACHE
        self.manual = os.path.join(self.cache, "plan_samples.jsonl")
        patch_default(self, overview.plan_samples, 1, self.manual)
        patch_default(self, overview.changes, 0, os.path.join(self.data_dir, "config", "changes.toml"))
        patch_default(self, overview.work_sections, 0, os.path.join(self.cache, "work_sections.json"))
        patch_default(self, overview.assign_work, 0, os.path.join(self.cache, "work_sections.json"))
        patch_default(self, alerts.notes, 0, os.path.join(self.cache, "review_notes.jsonl"))
        patch_default(self, alerts.record_note, 0, os.path.join(self.cache, "review_notes.jsonl"))
        overview._harvest_mem["cache"] = None
        harvest = functools.partial(overview.harvest_usage, root=self.projects,
                                    cache=os.path.join(self.cache, "usage_harvest.json"))
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
        # a subagent with its own tag differing from nothing else: own tag first
        subagent(pa, "sT", "t1", "ALPHA-own-tag", org=ORG_A, ago=600)
        # no evidence at all
        transcript(os.path.join(pa, "sU.jsonl"), "sU", None, "Unknown main", ago=1500)
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
        text = ('[settings]\nprojects_dir = %s\nextra_project_dirs = []\ndesktop_session_dirs = []\n'
                'plan_usage_history = ""\nallow_all_orgs = %s\n\n[settings.alerts]\nagent_cost = 0.000001\n'
                'machine_burn = 0.000001\n\n[orgs]\n"aaaa1111" = "Alpha Inc"\n' % (
                    json.dumps(self.projects), "true" if self.allow_all else "false"))
        with open(os.path.join(self.data_dir, "config", "devtime.toml"), "w", encoding="utf-8") as f:
            f.write(text + extra)


class Resolution(Fixture):
    def test_order_tag_desktop_parent_unknown(self):
        org = lambda i: self.by_id[i]["org"]
        self.assertEqual(org("sA"), ORG_A)          # own tag
        self.assertEqual(org("sT"), ORG_A)          # own tag beats the desktop index (which says B)
        self.assertEqual(org("sD"), ORG_B)          # no tag: desktop index
        self.assertEqual(org("x1"), ORG_A)          # subagent: its parent's
        self.assertEqual(org("d1"), ORG_B)          # subagent of a desktop-resolved session
        self.assertEqual(org("t1"), ORG_A)          # subagent with its own tag
        self.assertEqual(org("sU"), orgs.UNKNOWN)   # skills-plugin folder is skipped; no evidence
        self.assertEqual(self.store.default_org(), ORG_B)  # the most recently active

    def test_org_list_and_names(self):
        listing = self.store.org_list()
        self.assertEqual([e["id"] for e in listing], ["bbbb2222", "aaaa1111", "unknown"])
        self.assertEqual({e["id"]: e["name"] for e in listing}["aaaa1111"], "Alpha Inc")

    def test_resolve_prefix(self):
        self.assertEqual(self.store.resolve_org("aaaa"), ORG_A)
        self.assertEqual(self.store.resolve_org(ORG_B), ORG_B)
        self.assertEqual(self.store.resolve_org(""), ORG_B)
        with self.assertRaises(orgs.OrgError) as ctx:
            self.store.resolve_org("zzzz")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(orgs.OrgError) as ctx:
            self.store.resolve_org("all")
        self.assertEqual(ctx.exception.status, 403)

    def test_cache_version_bumped(self):
        from devtime import scan
        self.assertGreaterEqual(scan.CACHE_VERSION, 7)
        self.assertTrue(hasattr(next(iter(self.store.cache.states.values())), "org"))


class Readings(Fixture):
    def test_harvested_readings_per_org(self):
        settings = self.store.config.settings
        a = overview.plan_samples(settings, ORG_A)
        b = overview.plan_samples(settings, ORG_B)
        self.assertEqual([(round(s[1], 2), round(s[2], 2), s[5]) for s in a], [(0.11, 0.41, ORG_A)])
        self.assertEqual([(round(s[1], 2), round(s[2], 2), s[5]) for s in b], [(0.22, 0.52, ORG_B)])
        self.assertEqual(overview.plan_samples(settings, orgs.UNKNOWN), [])
        self.assertEqual(len(overview.plan_samples(settings)), 2)

    def test_manual_samples_are_unknown_unless_given(self):
        settings = self.store.config.settings
        overview.record_plan_sample(5, None, 60, None, path=self.manual)
        overview.record_plan_sample(7, None, 70, None, path=self.manual, org="aaaa1111")
        self.assertEqual([round(s[2], 2) for s in overview.plan_samples(settings, orgs.UNKNOWN)], [0.6])
        self.assertEqual(sorted(round(s[2], 2) for s in overview.plan_samples(settings, ORG_A)), [0.41, 0.7])
        self.assertEqual([round(s[2], 2) for s in overview.plan_samples(settings, ORG_B)], [0.52])

    def test_tick_reading_and_limits_are_per_org(self):
        now = NOW
        for org, week in ((ORG_A, 41.0), (ORG_B, 52.0)):
            out = overview.tick_summary(self.store.slice("*", org), self.store.config.settings, [], now, org)
            self.assertEqual(out["reading"]["weekly"], week)
        data = self.store.slice("*", ORG_A)
        plan = overview.limits(data, overview.Index(data), self.store.config.settings, NOW, org=ORG_A)
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

    def assert_only(self, body, mine, other):
        for word in mine:
            self.assertIn(word, body)
        for word in other:
            self.assertNotIn(word, body)

    THEIRS_OF_A = ("BRAVO", "Bravo", ORG_B, "bbbb2222", "sB", "sD", '"y1"', '"d1"')
    THEIRS_OF_B = ("ALPHA", "Alpha", ORG_A, "aaaa1111")

    def test_version_has_home(self):
        self.assertEqual(json.loads(self.text("/api/version"))["home"], paths.home_id())

    def test_orgs_endpoint(self):
        listing = json.loads(self.text("/api/orgs"))
        self.assertEqual([e["id"] for e in listing], ["bbbb2222", "aaaa1111", "unknown"])
        self.assertEqual(set(listing[0]), {"id", "name", "sessions", "last"})

    def test_default_is_most_recent_org(self):
        body = self.text("/api/watch")
        self.assert_only(body, ["Bravo main"], self.THEIRS_OF_B)

    def test_scoped_to_a(self):
        for path in ("/api/overview?org=aaaa1111&range=7d", "/api/watch?org=aaaa1111&hours=6",
                     "/api/alerts?org=aaaa1111", "/api/projects?org=aaaa1111",
                     "/api/tick?org=" + ORG_A, "/api/data?org=aaaa"):
            body = self.text(path)
            for word in self.THEIRS_OF_A:
                self.assertNotIn(word, body, (path, word))
        self.assert_only(self.text("/api/watch?org=aaaa1111&hours=6"), ["Alpha main", "ALPHA-task"], [])
        self.assert_only(self.text("/api/alerts?org=aaaa1111"), ["ALPHA-task"], [])
        self.assert_only(self.text("/api/overview?org=aaaa1111&range=7d"), ["ALPHA-task"], [])
        data = json.loads(self.text("/api/data?org=aaaa1111"))
        self.assertEqual({t["org"] for t in data["transcripts"]}, {ORG_A})
        self.assertEqual(sum(p["sessions"] for p in data["projects"]), 2)

    def test_scoped_to_b(self):
        for path in ("/api/overview?org=bbbb2222&range=7d", "/api/watch?org=bbbb2222&hours=6",
                     "/api/alerts?org=bbbb2222", "/api/projects?org=bbbb2222", "/api/data?org=bbbb2222"):
            body = self.text(path)
            for word in self.THEIRS_OF_B:
                self.assertNotIn(word, body, (path, word))
        self.assert_only(self.text("/api/alerts?org=bbbb2222"), ["BRAVO-task"], [])

    def test_alerts_carry_org_and_machine_burn_is_per_org(self):
        items = json.loads(self.text("/api/alerts?org=aaaa1111"))["alerts"]
        self.assertTrue(items)
        self.assertEqual({a["org"] for a in items}, {ORG_A})
        burns = [a for a in self.store.alerts.list() if a["kind"] == "machine burn"]
        self.assertEqual({a["org"] for a in burns}, {ORG_A, ORG_B, orgs.UNKNOWN})

    def test_work_items_scoped(self):
        for org, mine, theirs in ((ORG_A, "ALPHA-task", "BRAVO"), (ORG_B, "BRAVO-task", "ALPHA")):
            out = overview.work_items(self.store.slice("*", org), NOW, 8, 100, assign={})
            body = json.dumps(out)
            self.assertIn(mine, body)
            self.assertNotIn(theirs, body)

    def test_unknown_org_is_its_own_scope(self):
        data = json.loads(self.text("/api/data?org=unknown"))
        self.assertEqual([t["id"] for t in data["transcripts"]], ["sU"])

    def test_all_refused_unless_allowed(self):
        for path in ("/api/overview?org=all", "/api/data?org=all", "/api/watch?org=all", "/api/alerts?org=all",
                     "/api/tick?org=all", "/api/projects?org=all"):
            status, body = get(self.base, path)
            self.assertEqual(status, 403, path)
            self.assertNotIn("BRAVO", body)
        self.assertEqual(get(self.base, "/api/overview?org=nope")[0], 404)

    def test_tick_by_session(self):
        out = json.loads(self.text("/api/tick?session=sA"))
        self.assertEqual(out["org"], ORG_A)
        self.assertNotIn("orgNote", out)
        out = json.loads(self.text("/api/tick?session=sD"))
        self.assertEqual(out["org"], ORG_B)
        out = json.loads(self.text("/api/tick?session=${CLAUDE_SESSION_ID}"))
        self.assertEqual(out["org"], ORG_B)  # fallback: the most recent
        self.assertIn("orgNote", out)

    def test_local_tick_numbers_by_session(self):
        with mock.patch.object(tick, "local_store", return_value=self.store):
            out = tick.tick_numbers("http://x/", False, NOW, session="sA")
            self.assertEqual((out["org"], out["reading"]["weekly"]), (ORG_A, 41.0))
            out = tick.tick_numbers("http://x/", False, NOW, session="nope")
            self.assertEqual(out["org"], ORG_B)
            self.assertIn("orgNote", out)
            out = tick.tick_numbers("http://x/", False, NOW, org="aaaa")
            self.assertEqual(out["org"], ORG_A)


class AllowAll(Fixture):
    allow_all = True

    def test_all_allowed(self):
        self.assertIsNone(self.store.resolve_org("all"))
        self.assertEqual(len(self.store.slice("*", None)["transcripts"]), len(self.store.data["transcripts"]))


class Review(Fixture):
    def test_note_reaches_only_its_org(self):
        alerts.record_note("Alpha main", "check models", org="aaaa1111")
        alerts.record_note("Bravo main", "check waits", org="aaaa1111")  # no such session in org A
        current = alerts.check(self.store.data, self.store.config, time.time() + 1)
        notes = [a for a in current if a["kind"] == "usage review"]
        self.assertEqual([(a["owner"], a["org"]) for a in notes], [("Alpha main", ORG_A)])


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
        patcher = mock.patch.multiple(paths, DATA=data, CACHE=os.path.join(data, "cache"),
                                      USER_CONFIG=os.path.join(data, "config"))
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

    def test_home_id_depends_on_data_dir(self):
        first = paths.home_id()
        self.assertRegex(first, "^[0-9a-f]{12}$")
        with mock.patch.object(paths, "DATA", os.path.join(self.tmp.name, "other")):
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
