"""devtime slot watcher: learning slot rules from a (fake) process table, plus the matcher / add-rule / launcher fixes.

    python -m unittest tests.test_slotwatch
"""

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isolate  # noqa: E402,F401  (before devtime: a throw-away home)
from devtime import paths, slotrules, slots, slotwatch  # noqa: E402

GITBASH = r"C:\Program Files\Git\bin\bash.exe"
USRBASH = r"C:\Program Files\Git\usr\bin\bash.exe"
UE = r"D:\UE\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"


def claude_bash(cmd):
    """The command line Claude Code's Bash tool runs a command with."""
    return '"%s" -c "source /c/Users/u/.claude/shell-snapshots/snapshot-bash-1.sh && eval \'%s\' < /dev/null && ' \
           'pwd -P >| /tmp/claude-1-cwd"' % (GITBASH, cmd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="devtime-watch-")
        self.proj = os.path.join(self.tmp, "CityGen").replace("\\", "/")
        os.makedirs(os.path.join(self.proj, ".git"))
        os.makedirs(paths.USER_CONFIG, exist_ok=True)
        self.user = slotrules.user_file()
        self.write_user("")
        self.patch = mock.patch.dict(os.environ, {"DEVTIME_SLOTS_DIR": os.path.join(self.tmp, "q")})
        self.patch.start()
        slotrules._CONFIG = None

    def tearDown(self):
        self.patch.stop()
        slotrules._CONFIG = None
        for p in (self.user, slotrules.learned_file()):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(self.tmp, True)

    def write_user(self, text):
        with open(self.user, "w", encoding="utf-8") as f:
            f.write(text)

    def table(self, chain, cwd=None, extra=()):
        """chain: outermost first, (name, cmdline) pairs under a claude.exe (pid 10); pids 11, 12 ..."""
        rows = [{"pid": 10, "ppid": 1, "name": "claude.exe", "cmdline": "claude.exe"}]
        for i, (name, cmd) in enumerate(chain):
            rows.append({"pid": 11 + i, "ppid": 10 + i, "name": name, "cmdline": cmd, "cwd": cwd or self.proj})
        return slotwatch.StaticTable(rows + list(extra))

    def scan(self, table, dry=False):
        return slotwatch.Watcher(table).scan(slotrules.Config(), dry=dry)

    def learned(self):
        return slotwatch.load_learned()

    def match(self, cmd, tool="Bash", cwd=None):
        m = slotrules.match(cmd, tool, cwd or self.proj, slotrules.Config())
        return m and "+".join(m["resources"])

    def capture(self, script="Tools/Dev/capture_crowd.sh", args=" --n 3", heavy=("UnrealEditor-Cmd.exe", UE + " P.uproject")):
        return self.table([("bash.exe", claude_bash("bash %s%s" % (script, args))),
                           ("bash.exe", '"%s" %s%s' % (USRBASH, script, args)), heavy])


class Learning(Base):
    def test_learns_script_per_watch_entry(self):
        [r] = self.scan(self.capture())
        self.assertEqual((r["status"], r["script"], r["project"], r["resource"]),
                         ("learn", "Tools/Dev/capture_crowd.sh", "CityGen", "test"))
        [rule] = self.learned()
        self.assertTrue(rule["learned"])
        self.assertEqual(rule["seen"], 1)
        self.assertNotIn("--n", json.dumps(rule))  # no command arguments stored
        for cmd in ("bash Tools/Dev/capture_crowd.sh", "./Tools/Dev/capture_crowd.sh x",
                    "D:/dev/CityGen/Tools/Dev/capture_crowd.sh", 'bash "Tools\\Dev\\capture_crowd.sh"'):
            self.assertEqual(self.match(cmd), "test", cmd)
        for cmd in ("cat Tools/Dev/capture_crowd.sh", "bash Tools/Dev/capture_crowd.sh.bak", "bash my_capture_crowd.sh"):
            self.assertIsNone(self.match(cmd), cmd)
        ev = [x for x in slots.Queue().read_log(0) if x["event"] == "learned"]
        self.assertEqual((len(ev), ev[0]["label"], ev[0]["project"]), (1, "Tools/Dev/capture_crowd.sh", "CityGen"))
        # blender -b under another script: gpu; an interactive blender is not watched
        t = self.capture("Tools/Dev/render_town.sh", "", ("blender.exe", "blender.exe -b town.blend -P r.py"))
        self.assertEqual(self.scan(t)[0]["resource"], "gpu")
        t = self.capture("Tools/Dev/open_town.sh", "", ("blender.exe", "blender.exe town.blend"))
        self.assertEqual(self.scan(t), [])
        self.assertEqual(self.match("bash Tools/Dev/render_town.sh"), "gpu")

    def test_skips_queued_unrelated_and_nested_heavy(self):
        wrapped = self.table([("bash.exe", claude_bash("x")),
                              ("python.exe", 'python C:/Users/u/.claude/devtime/devtime.py slot run test --label "x" '
                                             '--shell bash'),
                              ("bash.exe", "bash Tools/Dev/capture_crowd.sh"), ("UnrealEditor-Cmd.exe", UE)])
        self.assertEqual(self.scan(wrapped)[0]["reason"], "already queued (slot run)")
        by_hand = slotwatch.StaticTable([
            {"pid": 5, "ppid": 1, "name": "WindowsTerminal.exe", "cmdline": "wt"},
            {"pid": 6, "ppid": 5, "name": "bash.exe", "cmdline": "bash"},
            {"pid": 7, "ppid": 6, "name": "bash.exe", "cmdline": "bash Tools/Dev/capture_crowd.sh", "cwd": self.proj},
            {"pid": 8, "ppid": 7, "name": "UnrealEditor-Cmd.exe", "cmdline": UE}])
        self.assertEqual(self.scan(by_hand)[0]["status"], "skip")
        ubt = self.capture(heavy=("UnrealEditor.exe", "UnrealEditor.exe P.uproject"))
        ubt.rows[14] = {"pid": 14, "ppid": 13, "name": "UnrealBuildTool.exe", "cmdline": "UnrealBuildTool.exe"}
        rs = {r["heavy"]: r for r in self.scan(ubt, dry=True)}
        self.assertEqual(rs["UnrealBuildTool.exe"]["reason"], "under UnrealEditor.exe")
        self.assertEqual(self.learned(), [])  # dry run

    def test_orphaned_script_shell_is_learned(self):
        t = slotwatch.StaticTable([
            {"pid": 7, "ppid": 99, "name": "bash.exe", "cmdline": '"%s" Tools/Dev/build_test.sh X' % USRBASH,
             "cwd": self.proj},
            {"pid": 8, "ppid": 7, "name": "UnrealEditor-Cmd.exe", "cmdline": UE}])
        [r] = self.scan(t)
        self.assertEqual((r["status"], r["script"], r.get("orphan")), ("learn", "Tools/Dev/build_test.sh", True))

    def test_refuses_generic_launchers(self):
        loop = self.table([("bash.exe", claude_bash('for i in 1 2; do "$UE" P.uproject; done')),
                           ("UnrealEditor-Cmd.exe", UE)])
        make = self.table([("bash.exe", claude_bash("make capture")), ("make.exe", "make capture"),
                           ("UnrealEditor-Cmd.exe", UE)])
        temp = self.table([("bash.exe", claude_bash("bash /tmp/x.sh")), ("bash.exe", "bash /tmp/x.sh"),
                           ("UnrealEditor-Cmd.exe", UE)])
        two = self.table([("bash.exe", claude_bash("bash Tools/a.sh; bash Tools/b.sh")), ("UnrealEditor-Cmd.exe", UE)])
        for t in (loop, make, temp, two):
            [r] = self.scan(t)
            self.assertEqual(r["status"], "unlearnable", r)
        self.assertEqual(self.learned(), [])
        rows = [x for x in slots.Queue().read_log(0) if x["event"] == "unlearnable"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1]["chain"], ["UnrealEditor-Cmd.exe", "make.exe", "bash.exe", "claude.exe"])
        self.assertNotIn("capture", json.dumps(rows[1]))  # no command text in the log

    def test_nested_scripts_pick_outermost_project_script(self):
        t = self.table([("bash.exe", claude_bash("bash D:/tools/wrap.sh Tools/run_all.sh")),
                        ("bash.exe", "bash D:/tools/wrap.sh Tools/run_all.sh"),
                        ("bash.exe", "bash Tools/run_all.sh"), ("bash.exe", "bash Tools/Dev/capture_crowd.sh"),
                        ("UnrealEditor-Cmd.exe", UE)])
        [r] = self.scan(t)
        self.assertEqual(r["script"], "Tools/run_all.sh")

    def test_powershell_in_process_script(self):
        cmd = "& ./Tools/Dev/run_checks.ps1 -Fast"
        enc = base64.b64encode(cmd.encode("utf-16-le")).decode()
        for top in ('powershell.exe -NoProfile -NonInteractive -Command "%s"' % cmd,
                    "pwsh.exe -NoProfile -EncodedCommand %s" % enc):
            [r] = self.scan(self.table([("powershell.exe", top), ("UnrealEditor-Cmd.exe", UE)]), dry=True)
            self.assertEqual((r["status"], r["script"]), ("learn", "Tools/Dev/run_checks.ps1"), top)

    def test_quiet_pattern_and_quiet_if(self):
        [r] = self.scan(self.capture("Tools/Dev/perf_capture.sh", ""))
        self.assertEqual(r["resource"], "quiet")
        self.assertEqual(self.match("bash Tools/Dev/perf_capture.sh"), "quiet")
        # the same script is a timing run or a normal gpu run by its arguments: quiet_if decides at hook time
        t = self.capture("Tools/Dev/gpu_run.sh", " Tools/Dev/measure_frame.py",
                         ("blender.exe", "blender.exe -b -P Tools/Dev/measure_frame.py"))
        [r] = self.scan(t)
        self.assertEqual(r["resource"], "gpu")
        self.assertIn("measure", r["rule"]["quiet_if"])
        self.assertEqual(self.match("bash Tools/Dev/gpu_run.sh Tools/Dev/measure_frame.py"), "quiet")
        self.assertEqual(self.match("bash Tools/Dev/gpu_run.sh Tools/Dev/render_town.py"), "gpu")
        self.assertEqual(self.match("bash Tools/Dev/gpu_run.sh x.py && echo measure"), "gpu")  # its own command only

    def test_dedupe_cap_and_remove(self):
        w = slotwatch.Watcher(self.capture())
        w.scan(slotrules.Config())
        w.scan(slotrules.Config())  # the same process again: nothing new
        t = self.capture()
        t.rows[13]["start"] = 2  # a new run of it
        self.assertEqual(self.scan(t)[0]["status"], "covered")
        [rule] = self.learned()
        self.assertEqual(rule["seen"], 2)
        t = self.capture(heavy=("blender.exe", "blender.exe --background x.blend"))
        self.scan(t)
        self.assertEqual(self.learned()[0]["resource"], "test+gpu")
        self.write_user("[settings]\nlearn_max = 2\n")
        for name in ("a", "b", "c"):
            self.scan(self.capture("Tools/%s.sh" % name, ""))
        self.assertEqual([r["script"] for r in self.learned()], ["Tools/b.sh", "Tools/c.sh"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(slots.main(["rules", "--learned"]), 0)
        self.assertIn("[learned seen 1x", out.getvalue())
        self.assertIn("project=CityGen", out.getvalue())
        with redirect_stdout(io.StringIO()):
            slots.main(["remove-rules", "--learned", "--project", "Other"])
            self.assertEqual(len(self.learned()), 2)
            slots.main(["remove-rules", "--learned", "--project", "citygen"])
        self.assertEqual(self.learned(), [])

    def test_user_rule_wins(self):
        self.write_user("[[rule]]\nmatch = '''Tools.Dev.capture_crowd'''\nresource = \"none\"\n")
        [r] = self.scan(self.capture())
        self.assertEqual((r["status"], r["learned"]), ("covered", False))
        self.assertEqual(self.learned(), [])
        # a learned rule is checked after the user's: an exempting user rule added later still wins
        self.write_user("")
        self.scan(self.capture("Tools/Dev/capture_town.sh", ""))
        self.assertEqual(self.match("bash Tools/Dev/capture_town.sh"), "test")
        self.write_user("[[rule]]\nmatch = '''capture_town'''\nresource = \"none\"\n")
        self.assertIsNone(self.match("bash Tools/Dev/capture_town.sh"))

    def test_disabled(self):
        self.write_user("[settings]\nlearn = false\n")
        self.assertEqual(self.scan(self.capture()), [])
        self.assertEqual(self.learned(), [])

    def test_hook_sees_new_rule(self):
        launcher = "L/devtime.py"
        inp = {"command": "bash Tools/Dev/capture_crowd.sh"}
        self.assertIsNone(slotrules.hook_check("Bash", inp, self.proj, launcher))
        self.scan(self.capture())
        r = slotrules.hook_check("Bash", inp, self.proj, launcher)
        self.assertEqual(r[0], "wrap")
        self.assertIn("slot run test", r[1]["command"])

    def test_single_watcher_lock(self):
        d = os.path.join(self.tmp, "q")
        fd = slotwatch.take_lock(d)
        self.assertIsNotNone(fd)
        self.assertIsNone(slotwatch.take_lock(d))
        os.close(fd)
        fd = slotwatch.take_lock(d)
        self.assertIsNotNone(fd)
        os.close(fd)

    @unittest.skipUnless(os.name == "nt", "Windows process APIs")
    def test_live_table_reads_own_chain(self):
        t = slotwatch.WinTable()
        procs = t.procs()
        self.assertIn(os.getpid(), procs)
        self.assertEqual(procs[os.getpid()][0], os.getppid())
        self.assertEqual(slotwatch.base(t.argv(os.getpid())[0]), slotwatch.base(sys.executable))
        self.assertEqual(os.path.normcase(t.cwd(os.getpid()).rstrip("\\/")), os.path.normcase(os.getcwd()))
        self.assertIsNotNone(t.start(os.getpid()))


class Fixes(Base):
    def test_interpreter_option_values(self):
        self.write_user("[[rule]]\nmatch = '''Tools.Dev.build\\.ps1'''\nresource = \"build\"\n")
        for cmd in (r"powershell -NoProfile -ExecutionPolicy Bypass -File Tools\Dev\build.ps1",
                    "powershell -File Tools/Dev/build.ps1", "pwsh -ep Bypass -WindowStyle Hidden Tools/Dev/build.ps1",
                    r"powershell.exe -ExecutionPolicy Bypass Tools\Dev\build.ps1 -Config Dev"):
            self.assertEqual(self.match(cmd, "PowerShell"), "build", cmd)
            self.assertEqual(self.match(cmd, "Bash"), "build", cmd)
        self.write_user("[[rule]]\nmatch = '''fakeheavy'''\nresource = \"test\"\n")
        self.assertEqual(self.match("bash -o pipefail fakeheavy.sh"), "test")
        self.assertEqual(self.match("python -X utf8 fakeheavy.py"), "test")
        self.assertIsNone(self.match("python -c \"print('fakeheavy')\""))  # program code, not a command line
        self.assertIsNone(self.match("node -e \"require('fakeheavy')\""))
        # here-document bodies are data: python - <<'EOF' ... text naming a heavy program ... EOF
        self.assertIsNone(self.match("python - <<'EOF'\nprint(1)\nfakeheavy (in the text)\nEOF\necho done"))
        self.assertIsNone(self.match("cat > f.md <<-END\n\tfakeheavy x\n\tEND"))
        self.assertEqual(self.match("cat > f <<EOF\nx\nEOF\n./fakeheavy run"), "test")
        self.assertEqual(self.match("echo $((1 << 2))\n2\n./fakeheavy"), "test")

    def test_add_rule_round_trips_backslashes(self):
        rx = "Tools[\\\\/]Dev[\\\\/]x\\.sh"
        out = io.StringIO()
        with redirect_stdout(out):
            slots.main(["add-rule", "--project", "P", "--match", rx, "--resource", "gpu"])
        self.assertEqual(slotrules.Config().rules[0].match_text, rx)
        self.assertNotIn("note:", out.getvalue())
        self.assertEqual(self.match("bash 'Tools\\Dev\\x.sh'"), "gpu")
        self.assertEqual(self.match("bash Tools/Dev/x.sh"), "gpu")
        with redirect_stdout(out):
            slots.main(["add-rule", "--project", "P", "--match", "Tools[\\/]Dev", "--resource", "gpu"])
        self.assertIn("note:", out.getvalue())  # a halved regex is pointed out

    def test_launcher_uses_newest_installed_version(self):
        cache = os.path.join(self.tmp, "plugins", "cache", "devtime", "devtime")
        for v in ("0.5.0", "0.6.0", "0.10.1"):
            d = os.path.join(cache, v)
            os.makedirs(os.path.join(d, ".claude-plugin"))
            os.makedirs(os.path.join(d, "devtime"))
            with open(os.path.join(d, ".claude-plugin", "plugin.json"), "w") as f:
                json.dump({"name": "devtime", "version": v}, f)
            with open(os.path.join(d, "devtime", "__init__.py"), "w") as f:
                f.write("")
            with open(os.path.join(d, "devtime", "__main__.py"), "w") as f:
                f.write("def main(argv):\n    print(%r)\n    return 0\n" % v)
        data = os.path.join(self.tmp, "data")
        with mock.patch.object(paths, "DATA", data):
            paths.install_launcher(os.path.join(cache, "0.6.0"))
            paths.install_launcher(os.path.join(cache, "0.5.0"))  # an older session: no downgrade
            with open(os.path.join(data, "plugin_root.txt")) as f:
                self.assertTrue(f.read().strip().endswith("0.6.0"))
            launcher = paths.launcher_path()
        r = subprocess.run([sys.executable, launcher], capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(r.stdout.strip(), "0.10.1", r.stderr)  # newest beside the recorded root
        with open(os.path.join(data, "plugin_root.txt"), "w") as f:
            f.write(ROOT)  # a checkout: used as recorded
        r = subprocess.run([sys.executable, launcher, "slot", "test", "x"], capture_output=True, text=True,
                           cwd=self.tmp, env=dict(os.environ, PYTHONPATH=""))
        self.assertEqual(r.stdout.strip(), "null", r.stderr)


if __name__ == "__main__":
    unittest.main()
