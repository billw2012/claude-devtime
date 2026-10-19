"""devtime slots: FIFO queue, exclusivity, liveness (dead holders swept, caller gone, max hold) and the hook's rewrite.

    python -m unittest tests.test_slots
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import paths, slotrules, slots  # noqa: E402

NT = os.name == "nt"
PY = sys.executable


def load_hook():
    spec = importlib.util.spec_from_file_location("agent_budget_slots", os.path.join(ROOT, "hooks", "agent_budget.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def alive(pid):
    if NT:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie is dead
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def until(pred, timeout=8.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out waiting for " + what)


CONFIG = """
[resources]
test = 1
gpu = 2
[groups]
quiet = ["test", "gpu"]
[[rule]]
match = '''fakeheavy'''
resource = "test"
[[rule]]
match = '''fakegpu'''
resource = "gpu"
max_hold = 7
"""


class SlotsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="devtime-slots-")
        self.dir = os.path.join(self.tmp, "q")
        os.makedirs(paths.USER_CONFIG, exist_ok=True)
        with open(slotrules.user_file(), "w", encoding="utf-8") as f:
            f.write(CONFIG)
        self.env = dict(os.environ, DEVTIME_SLOTS_DIR=self.dir, DEVTIME_SLOTS_POLL="0.03", PYTHONPATH=ROOT)
        self.patch = mock.patch.dict(os.environ, {"DEVTIME_SLOTS_DIR": self.dir})
        self.patch.start()
        slotrules._CONFIG = None
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        self.patch.stop()
        slotrules._CONFIG = None
        os.remove(slotrules.user_file())
        shutil.rmtree(self.tmp, True)

    def path(self, name):
        return os.path.join(self.tmp, name).replace("\\", "/")

    def start(self, resource, code, *flags):
        p = subprocess.Popen([PY, "-m", "devtime", "slot", "run", resource, *flags, "--", PY, "-c", code],
                             cwd=ROOT, env=self.env, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def tickets(self):
        d = os.path.join(self.dir, "tickets")
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    out.append(json.loads(f.read()))
            except (OSError, ValueError):
                pass
        return out

    def queued(self, n, state=None):
        return until(lambda: len([t for t in self.tickets() if state is None or t["state"] == state]) >= n,
                     what="%d tickets" % n)

    def log(self):
        return slots.Queue().read_log(0)

    def gated(self, gate, name=""):
        """Child code: waits for the gate file, then appends name to order.txt."""
        return ("import os,time\nwhile not os.path.exists(%r): time.sleep(0.01)\n"
                "open(%r,'a').write(%r)" % (gate, self.path("order.txt"), name))

    def appender(self, name):
        return "open(%r,'a').write(%r)" % (self.path("order.txt"), name)

    def order(self):
        with open(self.path("order.txt")) as f:
            return f.read()


class Queueing(SlotsBase):
    def test_fifo_order(self):
        gate = self.path("go")
        h = self.start("test", self.gated(gate, "H"), "--label", "holder")
        self.queued(1, "holding")
        a = self.start("test", self.appender("A"))
        self.queued(2)
        b = self.start("test", self.appender("B"))
        self.queued(3)
        self.assertEqual([t["state"] for t in self.tickets()], ["holding", "waiting", "waiting"])
        open(gate, "w").close()
        for p in (h, a, b):
            self.assertEqual(p.wait(10), 0)
        self.assertEqual(self.order(), "HAB")
        events = [(r["event"], r["label"]) for r in self.log()]
        self.assertEqual(events[0], ("acquire", "holder"))
        self.assertEqual([e for e, _ in events].count("release"), 3)
        self.assertEqual(self.tickets(), [])

    def test_group_waits_for_all_and_nothing_overtakes(self):
        gate = self.path("go")
        h = self.start("test", self.gated(gate, "H"))
        self.queued(1, "holding")
        x = self.start("quiet", self.appender("X"))  # exclusive over test + gpu: waits for the holder
        self.queued(2)
        c = self.start("gpu", self.appender("C"))  # gpu has free slots, but X is ahead and wants gpu
        self.queued(3)
        time.sleep(0.15)
        self.assertEqual([t["state"] for t in self.tickets()], ["holding", "waiting", "waiting"])
        open(gate, "w").close()
        for p in (h, x, c):
            self.assertEqual(p.wait(10), 0)
        self.assertEqual(self.order(), "HXC")

    def test_eligible_rules(self):
        cfg = slotrules.Config(user=slotrules.user_file())
        hold = {"id": 1, "state": "holding", "need": {"gpu": 1}}
        excl = {"id": 2, "state": "waiting", "need": {"gpu": -1}}
        late = {"id": 3, "state": "waiting", "need": {"gpu": 1}}
        other = {"id": 4, "state": "waiting", "need": {"test": 1}}
        tickets = [hold, excl, late, other]
        self.assertFalse(slots.eligible(excl, tickets, cfg))  # needs both gpu slots
        self.assertFalse(slots.eligible(late, tickets, cfg))  # a free slot, but the exclusive ticket is ahead
        self.assertTrue(slots.eligible(other, tickets, cfg))  # another resource: not held up
        self.assertTrue(slots.eligible(excl, [excl, late], cfg))
        self.assertEqual(cfg.expand("quiet"), {"test": -1, "gpu": -1})
        self.assertEqual(cfg.expand("test+gpu", exclusive=True), {"test": -1, "gpu": -1})

    def test_nested_run_does_not_queue_for_held_resource(self):
        inner = "import subprocess,sys; sys.exit(subprocess.call([sys.executable,'-m','devtime','slot','run','test'," \
                "'--',sys.executable,'-c','import sys; sys.exit(3)']))"
        p = self.start("test", inner)
        self.assertEqual(p.wait(10), 3)  # limit 1 and already held by the parent: would deadlock if it queued


class Liveness(SlotsBase):
    def test_killed_holder_is_swept_and_its_job_dies(self):
        pidfile = self.path("child.pid")
        code = "import os,time\nopen(%r,'w').write(str(os.getpid()))\ntime.sleep(30)" % pidfile
        h = self.start("test", code)
        until(lambda: read(pidfile), what="child pid")
        child = int(read(pidfile))
        h.kill()
        h.wait()
        if NT:  # the Job Object's KILL_ON_JOB_CLOSE ends the child with its wrapper
            until(lambda: not alive(child), 5, "child to die with its wrapper")
        else:
            os.kill(child, 9)
        a = self.start("test", self.appender("A"))
        self.assertEqual(a.wait(10), 0)
        self.assertEqual(self.order(), "A")
        self.assertIn("swept", [r["event"] for r in self.log()])

    def test_caller_gone_while_queued(self):
        gate = self.path("go")
        h = self.start("test", self.gated(gate, "H"))
        self.queued(1, "holding")
        tickets = os.path.join(self.dir, "tickets")
        middle = ("import os,subprocess,sys,time\n"
                  "subprocess.Popen([sys.executable,'-m','devtime','slot','run','test','--',sys.executable,'-c',"
                  "%r], stderr=subprocess.DEVNULL)\n"
                  "while len(os.listdir(%r)) < 2: time.sleep(0.01)\n" % (self.appender("Z"), tickets))
        m = subprocess.Popen([PY, "-c", middle], cwd=ROOT, env=self.env)
        self.procs.append(m)
        self.assertEqual(m.wait(10), 0)
        until(lambda: "gone" in [r["event"] for r in self.log()], what="caller-gone exit")
        self.assertEqual(len(self.tickets()), 1)
        open(gate, "w").close()
        self.assertEqual(h.wait(10), 0)
        self.assertEqual(self.order(), "H")  # the orphaned ticket never ran

    def test_max_hold_kills_child_and_grandchild(self):
        gpid = self.path("grand.pid")
        grand = "import os,time; open(%r,'w').write(str(os.getpid())); time.sleep(30)" % gpid
        code = "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',%r])\ntime.sleep(30)" % grand
        t0 = time.time()
        p = self.start("test", code, "--max-hold", "0.05")  # 3 s: the grandchild must start first, even under load
        self.assertEqual(p.wait(15), slots.EXIT_TIMEOUT)
        self.assertLess(time.time() - t0, 20)
        until(lambda: read(gpid), what="grandchild pid")
        until(lambda: not alive(int(read(gpid))), 5, "grandchild killed with the job")
        self.assertIn("timeout", [r["event"] for r in self.log()])
        self.assertEqual(self.tickets(), [])

    def test_slot_kill(self):
        h = self.start("test", "import time; time.sleep(30)")
        held = self.queued(1, "holding")
        ticket = self.tickets()[0]["id"]
        self.assertTrue(held)
        r = subprocess.run([PY, "-m", "devtime", "slot", "kill", str(ticket)], cwd=ROOT, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(h.wait(10), slots.EXIT_KILLED)


class Dashboard(SlotsBase):
    def test_summary_and_wait_alert(self):
        queue = slots.Queue()
        queue.ensure()
        now = time.time()
        info = {"id": 0, "pid": os.getpid(), "need": {"gpu": 1}, "label": "capture", "cwd": self.tmp, "project": "P",
                "session": "s-1", "org": isolate.ORG, "state": "waiting", "queued": now - 1500}
        other = dict(info, label="secret", org="other-org", session="s-2", queued=now - 1600)
        with queue.lock():
            info["id"] = queue.next_id()
            mine = slots.Ticket(queue, info)
            other["id"] = queue.next_id()
            theirs = slots.Ticket(queue, other)
            queue.log("acquire", dict(info, id=99), waited_s=600)
        try:
            alerts = slots.wait_alerts(now, isolate.ORG)
            self.assertEqual([(a["kind"], a["session"]) for a in alerts], [("queue wait", "s-1")])  # not the other org's
            self.assertIn("has waited 25m for 'gpu'", alerts[0]["text"])
            s = slots.summary(now, 12, isolate.ORG)
            self.assertEqual(sorted(t["label"] for t in s["queue"]), ["(another organisation)", "capture"])
            self.assertGreater(s["waits"]["gpu"][-1], 10 + 25 + 26 - 1)  # the logged 10 min + the two still waiting
        finally:
            with queue.lock():
                mine.close()
                theirs.close()


class Rules(SlotsBase):
    def match(self, cmd, tool="Bash"):
        m = slotrules.match(cmd, tool, "D:/x", slotrules.Config())
        return m and "+".join(m["resources"])

    def test_program_position(self):
        self.assertEqual(self.match('"C:/UE 5/Engine/Binaries/Win64/UnrealEditor-Cmd.exe" P.uproject -run=x'), "test")
        self.assertEqual(self.match("cd /d/p && UnrealEditor-Cmd P.uproject 2>&1 | tail -20"), "test")
        self.assertEqual(self.match("bash Tools/fakeheavy.sh a b"), "test")
        self.assertEqual(self.match("timeout 600 python -u fakeheavy.py"), "test")
        self.assertEqual(self.match("FOO=1 ./fakegpu --x"), "gpu")
        self.assertEqual(self.match('cmd /c "D:\\UE\\Engine\\Build\\BatchFiles\\Build.bat" X Win64'), "build")
        self.assertEqual(self.match("./fakeheavy && ./fakegpu"), "test+gpu")
        self.assertEqual(self.match('$ue = "C:\\UE\\UnrealEditor-Cmd.exe"\n& $ue P.uproject', "PowerShell"), "test")
        self.assertEqual(self.match("blender -b scene.blend -P render.py"), "gpu")
        self.assertEqual(self.match("bash -c 'fakeheavy --x'"), "test")
        for cmd in ("grep -r UnrealEditor-Cmd Source/", "tasklist | findstr UnrealEditor", "cat Build.bat",
                    "git commit -m 'run fakeheavy'", "echo fakeheavy", "blender --version", "ls fakegpu"):
            self.assertIsNone(self.match(cmd), cmd)

    def test_background_start_is_refused(self):
        for tool, cmd in (("Bash", "./fakeheavy &"), ("PowerShell", "Start-Process fakeheavy.exe -ArgumentList x")):
            r = slotrules.hook_check(tool, {"command": cmd}, "D:/x", "L/devtime.py")
            self.assertEqual(r[0], "deny", cmd)
        r = slotrules.hook_check("PowerShell", {"command": "Start-Process fakeheavy.exe -Wait"}, "D:/x", "L")
        self.assertEqual(r[0], "wrap")

    def test_hand_rolled_waits(self):
        for cmd in ('until [ -z "$(tasklist | grep -i UnrealEditor-Cmd)" ]; do sleep 30; done',
                    "while pgrep -f blender >/dev/null; do sleep 5; done; ./render.sh",
                    "while (Get-Process UnrealEditor -ErrorAction SilentlyContinue) { Start-Sleep 10 }",
                    "while [ -e /d/dev/gpu.lock ]; do sleep 2; done"):
            self.assertTrue(slotrules.hand_rolled_wait(cmd), cmd)
        for cmd in ("tasklist | grep UnrealEditor", "while read l; do echo $l; done < f.txt", "sleep 2; ls"):
            self.assertIsNone(slotrules.hand_rolled_wait(cmd), cmd)

    def test_main_session_background_wait_allowed(self):
        loop = {"command": "while pgrep -f fakeheavy >/dev/null; do sleep 30; done", "run_in_background": True}
        self.assertIsNone(slotrules.hook_check("Bash", loop, "D:/x", "L", subagent=False))
        self.assertEqual(slotrules.hook_check("Bash", loop, "D:/x", "L", subagent=True)[0], "deny")
        fg = {"command": loop["command"]}
        self.assertEqual(slotrules.hook_check("Bash", fg, "D:/x", "L", subagent=False)[0], "deny")


class Hook(SlotsBase):
    def setUp(self):
        super().setUp()
        self.hook = load_hook()

    def call(self, cmd, tool="Bash", agent=None, **extra):
        data = {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": "s-1", "cwd": self.tmp,
                "tool_input": dict({"command": cmd, "description": 'Run "the" job'}, **extra)}
        if agent:
            data["agent_id"] = agent
        return self.hook.decide(data)

    def test_rewrite_and_run(self):
        script = os.path.join(self.tmp, "fakeheavy_job.py")
        with open(script, "w") as f:
            f.write("import os,sys\nprint('ran', sys.argv[1:], os.environ.get('DEVTIME_SLOT_HELD'), "
                    "'DEVTIME_SLOT_CMD' in os.environ)\nsys.exit(5)\n")
        cmd = "python %s \"a b\" 'c' $((1+1))" % script.replace("\\", "/")
        out = self.call(cmd, run_in_background=True)
        spec = out["hookSpecificOutput"]
        new = spec["updatedInput"]
        self.assertTrue(new["run_in_background"])
        self.assertEqual(new["description"], 'Run "the" job')
        self.assertIn("slot run test", new["command"])
        self.assertIn("--label 'Run the job'", new["command"])
        self.assertIn("queued for the machine-wide 'test' slot", spec["additionalContext"])
        self.assertNotIn("permissionDecision", spec)
        self.assertTrue(os.path.exists(paths.launcher_path()))
        bash = slots.find_bash()
        r = subprocess.run([bash, "-c", new["command"]], cwd=self.tmp, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("ran ['a b', 'c', '2'] test False", r.stdout)
        self.assertEqual([x["event"] for x in self.log()], ["acquire", "release"])
        self.assertEqual(self.log()[0]["cwd"].replace("\\", "/").lower(), self.tmp.replace("\\", "/").lower())

    @unittest.skipUnless(NT, "Windows PowerShell")
    def test_powershell_rewrite_runs(self):
        cmd = "$x = 'it''s'; Write-Output \"$x `\"q`\"\"; ./fakeheavy.exe; cmd /c exit 6"
        new = self.call(cmd, "PowerShell")["hookSpecificOutput"]["updatedInput"]["command"]
        head, rest = new.split("\n'@\n", 1)
        self.assertEqual(head, "$env:DEVTIME_SLOT_CMD = @'\n" + cmd)  # a literal here-string: nothing expanded
        self.assertRegex(rest, r"^& '[^']+' '[^']+devtime\.py' slot run test --label 'Run the job' --shell powershell$")
        r = subprocess.run([PY, "-m", "devtime", "slot", "run", "test", "--shell", "powershell"], cwd=ROOT,
                           env=dict(self.env, DEVTIME_SLOT_CMD=cmd), capture_output=True, text=True)
        self.assertEqual(r.returncode, 6, r.stderr)  # a missing command doesn't stop the script, as in the tool
        self.assertIn('it\'s "q"', r.stdout)

    def test_untouched(self):
        self.assertIsNone(self.call("ls -la && git status"))
        launcher = paths.launcher_path()
        self.assertIsNone(self.call('python "%s" slot run test -- ./fakeheavy' % launcher))
        self.assertIsNone(self.call("DEVTIME_SLOTS=0 ./fakeheavy"))
        with mock.patch.dict(os.environ, {"DEVTIME_SLOTS": "0"}):
            self.assertIsNone(self.call("./fakeheavy"))

    def test_waits_refused(self):
        loop = 'until [ -z "$(tasklist | grep -i UnrealEditor-Cmd)" ]; do sleep 30; done'
        out = self.call(loop)  # main session: pointed at slot run
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("slot run", out["hookSpecificOutput"]["permissionDecisionReason"])
        with mock.patch.dict(os.environ, {"DEVTIME_SLOTS": "0"}):  # a subagent: a long wait even with no timeout
            self.assertIsNone(self.call(loop))
            out = self.call(loop, agent="agent-x", timeout=60000)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("waiting for another job", out["hookSpecificOutput"]["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
