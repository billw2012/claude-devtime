"""Machine-wide resource slots: one FIFO queue for heavy jobs that every Claude Code session on the PC shares.

    python -m devtime slot run RES[+RES...] [--exclusive] [--max-hold MIN] [--label TEXT] [--shell bash|powershell]
                                [-- COMMAND ...]
    python -m devtime slot status [--json]
    python -m devtime slot kill TICKET
    python -m devtime slot log [--since 2h] [--json]
    python -m devtime slot rules [--project P] | test COMMAND [--tool Bash] [--cwd DIR]
    python -m devtime slot add-rule --project P --match RE --resource R [--cwd RE] [--exclusive] [--max-hold MIN]
    python -m devtime slot remove-rules --project P

`run` takes a ticket, waits its turn, runs the command as a child and releases when it exits, with the child's exit
code (or EXIT_TIMEOUT / EXIT_CALLER_GONE / EXIT_KILLED below). With --shell the command is a shell command line taken
from env DEVTIME_SLOT_CMD (how the hook wraps commands) or from the words after --.

Liveness: every ticket is a file in <dir>/tickets locked (msvcrt.locking / fcntl.flock on an open handle) by the live
`slot` process that owns it, and holding slots means owning a ticket in state "holding". A ticket whose lock can be
taken belongs to a dead process (crash, kill, closed session, reboot; PID reuse can't fool it) and the next process
that looks at the queue deletes it. On Windows the child runs in a Job Object with KILL_ON_JOB_CLOSE, so when the slot
process dies, its whole job (the editor, the test runner, their children) dies with it: no job runs while its slot is
free. Every change to the queue happens under <dir>/queue.lock (held for milliseconds).

Fairness: strict FIFO per resource. A ticket may start only when no older waiting ticket wants any of its resources
and each has room; an exclusive or group request waits for every slot of its resources, and nothing queued behind it
overtakes (no starvation).
"""

import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import paths, slotrules

EXIT_KILLED = 123        # stopped by `slot kill`
EXIT_TIMEOUT = 124       # killed after holding its slots past max-hold
EXIT_CALLER_GONE = 125   # the process that started it went away while it was still queued
EXIT_NOT_FOUND = 127
EXIT_USAGE = 2
LOCK_AT = 1 << 30        # the locked byte, far past the content so readers are never blocked (Windows locks are
                         # mandatory)
HELD_ENV = "DEVTIME_SLOT_HELD"
CMD_ENV = "DEVTIME_SLOT_CMD"
O_BIN = getattr(os, "O_BINARY", 0)
NT = os.name == "nt"

if NT:
    import msvcrt

    def try_lock(fd):
        os.lseek(fd, LOCK_AT, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def unlock(fd):
        os.lseek(fd, LOCK_AT, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


def err(text):
    sys.stderr.write("devtime slot: %s\n" % text)
    sys.stderr.flush()


class Queue:
    """The lock directory: queue.lock, seq, tickets/, kill/, log.jsonl."""

    def __init__(self, cfg=None):
        self.cfg = cfg or slotrules.Config()
        self.dir = self.cfg.lock_dir()
        self.tickets = os.path.join(self.dir, "tickets")
        self.kills = os.path.join(self.dir, "kill")
        self.log_path = os.path.join(self.dir, "log.jsonl")

    def ensure(self):
        os.makedirs(self.tickets, exist_ok=True)
        os.makedirs(self.kills, exist_ok=True)

    def lock(self):
        return _QueueLock(os.path.join(self.dir, "queue.lock"))

    def next_id(self):
        path = os.path.join(self.dir, "seq")
        try:
            with open(path, encoding="ascii") as f:
                n = int(f.read().strip() or 0)
        except (OSError, ValueError):
            n = 0
        n = max(n, max([int(x.split(".")[0]) for x in os.listdir(self.tickets) if x[:1].isdigit()] or [0])) + 1
        with open(path, "w", encoding="ascii") as f:
            f.write(str(n))
        return n

    def scan(self, own=None):
        """Live tickets (dicts) by id; dead ones are deleted and logged. Call under the queue lock."""
        out = []
        for name in sorted(os.listdir(self.tickets)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.tickets, name)
            if own is not None and path == own.path:
                out.append(own.info)
                continue
            try:
                fd = os.open(path, os.O_RDWR | O_BIN)
            except OSError:
                continue
            dead = False
            try:
                raw = _read_fd(fd)
                if try_lock(fd):
                    unlock(fd)
                    dead = True
            finally:
                os.close(fd)
            try:
                info = json.loads(raw.decode("utf-8") or "null")
            except ValueError:
                info = None
            if dead:
                try:
                    os.remove(path)
                except OSError:
                    pass
                if isinstance(info, dict):
                    self.log("swept", info)
                continue
            if isinstance(info, dict):
                out.append(info)
        live = {str(t["id"]) for t in out}
        for name in os.listdir(self.kills):
            if name not in live:
                try:
                    os.remove(os.path.join(self.kills, name))
                except OSError:
                    pass
        return sorted(out, key=lambda t: t["id"])

    def log(self, event, info, **extra):
        row = {"t": round(time.time(), 1), "event": event, "ticket": info.get("id"),
               "resource": "+".join(sorted(info.get("need") or {})), "label": info.get("label"),
               "cwd": info.get("cwd"), "project": info.get("project"), "session": info.get("session"),
               "org": info.get("org"), "pid": info.get("pid")}
        row.update(extra)
        try:
            limit = float(self.cfg.settings.get("log_kb", 512)) * 1024
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > limit:
                os.replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def read_log(self, since=0.0):
        rows = []
        for path in (self.log_path + ".1", self.log_path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("t", 0) >= since:
                            rows.append(r)
            except OSError:
                pass
        return rows

    def snapshot(self):
        """Live tickets right now (sweeping dead ones), or [] when the queue doesn't exist yet."""
        if not os.path.isdir(self.tickets):
            return []
        with self.lock():
            return self.scan()


class _QueueLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | O_BIN, 0o666)
        delay, deadline = 0.002, time.time() + 60
        while not try_lock(self.fd):
            if time.time() > deadline:
                os.close(self.fd)
                raise OSError("queue lock %s busy for 60 s" % self.path)
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
        return self

    def __exit__(self, *exc):
        try:
            unlock(self.fd)
        finally:
            os.close(self.fd)


def _read_fd(fd):
    os.lseek(fd, 0, 0)
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks).rstrip(b" \x00\r\n")


class Ticket:
    """Our own ticket file, locked for as long as this process lives."""

    def __init__(self, queue, info):
        self.queue = queue
        self.info = info
        self.path = os.path.join(queue.tickets, "%09d.json" % info["id"])
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | O_BIN, 0o666)
        if not try_lock(self.fd):
            raise OSError("cannot lock new ticket %s" % self.path)
        self.size = 0
        self.write()

    def write(self):
        data = json.dumps(self.info, separators=(",", ":")).encode("utf-8")
        os.lseek(self.fd, 0, 0)
        os.write(self.fd, data + b" " * max(0, self.size - len(data)))
        self.size = max(self.size, len(data))

    def close(self):
        """Releases the lock and deletes the file (call under the queue lock)."""
        if self.fd is None:
            return
        try:
            unlock(self.fd)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass


def eligible(me, tickets, cfg):
    """True when `me` may take its slots now: FIFO per resource, room on each."""
    for r, need in me["need"].items():
        limit = cfg.limit(r)
        for t in tickets:
            if t["id"] == me["id"]:
                continue
            if t.get("state") == "waiting" and t["id"] < me["id"] and r in t.get("need", {}):
                return False
        used = sum(limit if n < 0 else n for t in tickets if t.get("state") == "holding" and t["id"] != me["id"]
                   for rr, n in t.get("need", {}).items() if rr == r)
        if used + (limit if need < 0 else need) > limit:
            return False
    return True


def project_of(cwd):
    d = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d) or d
        up = os.path.dirname(d)
        if up == d:
            return os.path.basename(os.path.abspath(cwd)) or cwd
        d = up


# ----------------------------------------------------------------------------------------- processes (Windows)

if NT:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _created(handle):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime

    def make_job():
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(job)
            return None
        return job


class Caller:
    """The process that started us (the tool's shell). alive() is False once it has exited."""

    def __init__(self):
        self.ppid = os.getppid()
        self.handle = None
        if NT:
            h = _k32.OpenProcess(0x00100000 | 0x1000, False, self.ppid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if h:
                parent, me = _created(h), _created(_k32.GetCurrentProcess())
                if parent is not None and me is not None and parent > me:  # the PID was reused: our parent is gone
                    _k32.CloseHandle(h)
                    self.handle = "gone"
                else:
                    self.handle = h
            elif ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such process
                self.handle = "gone"

    def alive(self):
        if NT:
            if self.handle == "gone":
                return False
            return self.handle is None or _k32.WaitForSingleObject(self.handle, 0) == 0x102  # WAIT_TIMEOUT
        return os.getppid() == self.ppid


class Child:
    """The job: on Windows in a Job Object (its whole tree dies with us or on kill()); on POSIX in its own process
    group."""

    def __init__(self, argv, env):
        self.job = None
        if NT:
            job = make_job()
            self.proc = subprocess.Popen(argv, env=env, creationflags=0x4 if job else 0)  # CREATE_SUSPENDED
            if job:
                handle = int(self.proc._handle)
                if _k32.AssignProcessToJobObject(job, handle):
                    self.job = job
                else:
                    _k32.CloseHandle(job)
                _ntdll.NtResumeProcess(handle)
        else:
            self.proc = subprocess.Popen(argv, env=env, start_new_session=True)

    def wait(self, timeout):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self, code=1):
        if NT:
            if self.job:
                _k32.TerminateJobObject(self.job, code)
            else:
                self.proc.kill()
        else:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except OSError:
                    break
                if self.wait(3) is not None:
                    break
        self.wait(10)

    def close(self):
        """After the child exited: ends whatever it left running in its job (Windows) or group (POSIX)."""
        if NT and self.job:
            _k32.TerminateJobObject(self.job, 1)
            _k32.CloseHandle(self.job)
            self.job = None
        elif not NT:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass


def find_bash():
    exe = os.environ.get("SHELL") if not NT else None
    if exe and os.path.basename(exe) in ("bash", "zsh") and os.path.exists(exe):
        return exe
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for name in (("bash.exe",) if NT else ("bash",)):
            p = os.path.join(d, name)
            if os.path.isfile(p) and not (NT and "system32" in p.lower()):  # System32\bash.exe is WSL
                return p
    return shutil.which("bash") or "bash"


def command_argv(args, env):
    """argv of the child, and the env it gets."""
    if args.shell:
        script = env.pop(CMD_ENV, None)
        if env.get("MSYS2_ENV_CONV_EXCL") == CMD_ENV:
            env.pop("MSYS2_ENV_CONV_EXCL")
        if script is None:
            script = " ".join(args.command)
        if not script.strip():
            return None, env
        env[CMD_ENV] = script
        if args.shell == "bash":
            return [find_bash(), "-c", 'c=$%s; unset %s; eval "$c"' % (CMD_ENV, CMD_ENV)], env
        exe = "powershell.exe" if args.shell == "powershell" and NT else "pwsh"
        ps = ("$c = $env:%s; Remove-Item Env:%s; . ([scriptblock]::Create($c)); if ($LASTEXITCODE) { exit $LASTEXITCODE } "
              "elseif (-not $?) { exit 1 }" % (CMD_ENV, CMD_ENV))
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps], env
    argv = list(args.command)
    if not argv:
        return None, env
    found = shutil.which(argv[0])
    if found:
        argv[0] = found
    return argv, env


def fmt_s(s):
    s = int(max(0, s))
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh%02d" % (s // 3600, s % 3600 // 60)


def describe(t, now):
    who = t.get("label") or "job"
    where = t.get("project") or ""
    return "#%s '%s'%s" % (t["id"], who, " (%s)" % where if where else "")


def run(args):
    cfg = slotrules.Config()
    need = cfg.expand(args.resource, args.exclusive)
    if not need:
        err("no resource given")
        return EXIT_USAGE
    env = dict(os.environ)
    held = set(filter(None, env.get(HELD_ENV, "").split(",")))
    need = {r: n for r, n in need.items() if r not in held}  # an ancestor `slot run` already holds these
    argv, env = command_argv(args, env)
    if argv is None:
        err("no command given")
        return EXIT_USAGE
    if not need:
        return _run_child(argv, env, None, None, 0)
    queue = Queue(cfg)
    max_hold = args.max_hold if args.max_hold is not None else cfg.max_hold_min(need)
    cwd = os.getcwd()
    info = {"id": 0, "pid": os.getpid(), "need": need, "label": args.label or " ".join((env.get(CMD_ENV) or " ".join(args.command)).split())[:80] or "job",
            "cwd": cwd, "project": project_of(cwd), "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
            "org": paths.ORG, "state": "waiting", "queued": round(time.time(), 1), "max_hold_min": max_hold}
    caller = Caller()
    try:
        queue.ensure()
        with queue.lock():
            info["id"] = queue.next_id()
            ticket = Ticket(queue, info)
    except OSError as e:  # no lock dir (sandbox, permissions): never block the work, run it unqueued
        err("queue unavailable (%s); running without a slot" % e)
        return _run_child(argv, env, None, None, 0)
    env[HELD_ENV] = ",".join(sorted(held | set(need)))
    env["DEVTIME_SLOT_TICKET"] = str(info["id"])
    poll = float(os.environ.get("DEVTIME_SLOTS_POLL") or cfg.settings.get("poll_s", 1.0))
    kill_marker = os.path.join(queue.kills, str(info["id"]))
    told = 0.0
    try:
        while True:
            with queue.lock():
                tickets = queue.scan(own=ticket)
                if eligible(info, tickets, cfg):
                    info["state"] = "holding"
                    info["acquired"] = round(time.time(), 1)
                    ticket.write()
                    waited = info["acquired"] - info["queued"]
                    queue.log("acquire", info, waited_s=round(waited, 1))
                    break
            now = time.time()
            if os.path.exists(kill_marker):
                with queue.lock():
                    queue.log("kill", info, waited_s=round(now - info["queued"], 1), state="waiting")
                    ticket.close()
                err("ticket #%s removed by `slot kill`" % info["id"])
                return EXIT_KILLED
            if not caller.alive():
                with queue.lock():
                    queue.log("gone", info, waited_s=round(now - info["queued"], 1))
                    ticket.close()
                return EXIT_CALLER_GONE
            if now - told >= 600:
                mine = set(need)
                holders = [t for t in tickets if t.get("state") == "holding" and mine & set(t.get("need", {}))]
                ahead = [t for t in tickets if t.get("state") == "waiting" and t["id"] < info["id"]
                         and mine & set(t.get("need", {}))]
                err("#%s waiting for %s (%s%s)%s" % (
                    info["id"], "+".join(sorted(need)),
                    ", ".join("%s holds %s" % (describe(t, now), fmt_s(now - t.get("acquired", now))) for t in holders)
                    or "free soon", "; %d ahead" % len(ahead) if ahead else "",
                    "" if told else "; `slot status` shows the queue"))
                told = now
            time.sleep(poll)
        if told:
            err("#%s got %s after %s" % (info["id"], "+".join(sorted(need)), fmt_s(waited)))
        return _run_child(argv, env, queue, ticket, max_hold, kill_marker, poll)
    except KeyboardInterrupt:
        with queue.lock():
            queue.log("gone", info, interrupted=True)
            ticket.close()
        return 130
    finally:
        if ticket.fd is not None:
            try:
                with queue.lock():
                    ticket.close()
            except OSError:
                pass


def _run_child(argv, env, queue, ticket, max_hold, kill_marker=None, poll=1.0):
    try:
        child = Child(argv, env)
    except OSError as e:
        err("cannot start %s: %s" % (argv[0], e))
        code, event = EXIT_NOT_FOUND, "release"
        if queue:
            with queue.lock():
                queue.log(event, ticket.info, held_s=0, exit=code)
                ticket.close()
        return code
    if queue is None:
        try:
            code = child.proc.wait()
        except KeyboardInterrupt:
            child.kill()
            code = 130
        child.close()
        return code
    info = ticket.info
    start = time.time()
    event = "release"
    if not NT:
        def stop(signum, frame):
            raise KeyboardInterrupt
        for s in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(s, stop)
            except (OSError, ValueError):
                pass
    try:
        while True:
            code = child.wait(poll)
            if code is not None:
                break
            if max_hold and time.time() - start > max_hold * 60:
                child.kill(EXIT_TIMEOUT)
                code, event = EXIT_TIMEOUT, "timeout"
                err("#%s held %s past its max hold of %g min: killed its process tree" % (
                    info["id"], "+".join(sorted(info["need"])), max_hold))
                break
            if kill_marker and os.path.exists(kill_marker):
                child.kill(EXIT_KILLED)
                code, event = EXIT_KILLED, "kill"
                err("#%s stopped by `slot kill`" % info["id"])
                break
    except KeyboardInterrupt:
        child.kill(130)
        code, event = 130, "kill"
    child.close()
    with queue.lock():
        queue.log(event, info, waited_s=round(info["acquired"] - info["queued"], 1),
                  held_s=round(time.time() - start, 1), exit=code)
        ticket.close()
    return code


# ------------------------------------------------------------------------------------------------- other commands

def status(args):
    cfg = slotrules.Config()
    queue = Queue(cfg)
    tickets = queue.snapshot()
    now = time.time()
    names = list(cfg.resources) + sorted({r for t in tickets for r in t.get("need", {})} - set(cfg.resources))
    if args.json:
        print(json.dumps({"dir": queue.dir, "now": now, "resources": {n: cfg.limit(n) for n in names},
                          "groups": cfg.groups, "tickets": tickets}, separators=(",", ":")))
        return 0
    lines = []
    for n in names:
        limit = cfg.limit(n)
        hold = [t for t in tickets if t.get("state") == "holding" and n in t.get("need", {})]
        wait = [t for t in tickets if t.get("state") == "waiting" and n in t.get("need", {})]
        used = sum(limit if t["need"][n] < 0 else t["need"][n] for t in hold)
        line = "%-6s %d/%d" % (n, used, limit)
        if hold:
            line += "  held: " + ", ".join("%s %s" % (describe(t, now), fmt_s(now - t.get("acquired", now)))
                                          for t in hold)
        if wait:
            line += "  queue: " + ", ".join("%s%s %s" % (describe(t, now), " excl" if t["need"][n] < 0 else "",
                                                         fmt_s(now - t.get("queued", now))) for t in wait)
        lines.append(line)
    lines.append("groups: " + ", ".join("%s=%s" % (g, "+".join(r)) for g, r in cfg.groups.items()) +
                 "   (dir %s)" % queue.dir)
    print("\n".join(lines))
    return 0


def kill(args):
    queue = Queue()
    tickets = queue.snapshot()
    hit = [t for t in tickets if str(t["id"]) == str(args.ticket).lstrip("#")]
    if not hit:
        err("no live ticket #%s" % args.ticket)
        return 1
    with open(os.path.join(queue.kills, str(hit[0]["id"])), "w", encoding="ascii") as f:
        f.write(str(time.time()))
    print("asked #%s (%s, %s) to stop; it ends within a few seconds" % (hit[0]["id"], hit[0].get("label"),
                                                                       hit[0].get("state")))
    return 0


def parse_since(text):
    if not text:
        return time.time() - 86400
    units = {"m": 60, "h": 3600, "d": 86400}
    if text[-1] in units and text[:-1].replace(".", "", 1).isdigit():
        return time.time() - float(text[:-1]) * units[text[-1]]
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def log_cmd(args):
    rows = Queue().read_log(parse_since(args.since))
    if args.json:
        print(json.dumps(rows, separators=(",", ":")))
        return 0
    for r in rows[-args.tail:]:
        t = datetime.datetime.fromtimestamp(r["t"]).strftime("%m-%d %H:%M:%S")
        extra = " ".join("%s=%s" % (k, r[k]) for k in ("waited_s", "held_s", "exit") if r.get(k) is not None)
        print("%s %-7s #%s %-10s %s (%s) %s" % (t, r["event"], r.get("ticket"), r.get("resource"), r.get("label"),
                                                r.get("project"), extra))
    return 0


def rules_cmd(args):
    cfg = slotrules.Config()
    for r in cfg.rules:
        if args.project and str(r.project or "").lower() != args.project.lower():
            continue
        print("%-8s %s%s%s%s  [%s]" % (r.resource, r.match_text, " exclusive" if r.exclusive else "",
                                       " cwd~%s" % r.cwd.pattern if r.cwd else "",
                                       " project=%s" % r.project if r.project else "",
                                       "user" if r.source != slotrules.SHIPPED else "shipped"))
    return 0


def test_cmd(args):
    cfg = slotrules.Config()
    m = slotrules.match(args.command_text, args.tool, args.cwd or os.getcwd(), cfg)
    print(json.dumps(m))
    return 0


BEGIN, END = "# >>> devtime-slots project=%s", "# <<< devtime-slots"


def _toml_str(s):
    return "'''%s'''" % s if "'''" not in s and not s.endswith("'") else json.dumps(s)


def add_rule(args):
    import re
    import tomllib
    try:
        re.compile(args.match)
    except re.error as e:
        err("bad --match regex: %s" % e)
        return EXIT_USAGE
    path = slotrules.user_file()
    block = [BEGIN % args.project, "[[rule]]", "match = %s" % _toml_str(args.match),
             "resource = %s" % json.dumps(args.resource), "project = %s" % json.dumps(args.project)]
    if args.cwd:
        block.append("cwd = %s" % _toml_str(args.cwd))
    if args.exclusive:
        block.append("exclusive = true")
    if args.max_hold is not None:
        block.append("max_hold = %g" % args.max_hold)
    block.append(END)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ("# Your devtime slot settings and rules (machine-wide; defaults and reference: the plugin's "
                "config/slots.toml)\n")
    new = text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
    try:
        tomllib.loads(new)
    except ValueError as e:
        err("the rule would make %s invalid TOML: %s" % (path, e))
        return 1
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    print("added to %s: %s -> %s" % (path, args.match, args.resource))
    return 0


def remove_rules(args):
    path = slotrules.user_file()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        print("no rules file %s" % path)
        return 0
    out, skip, n = [], False, 0
    for line in lines:
        if line.strip().lower() == (BEGIN % args.project).lower():
            skip, n = True, n + 1
            continue
        if skip:
            skip = line.strip() != END
            continue
        out.append(line)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    print("removed %d rule(s) of project %s from %s (hand-written rules are left as they are)" % (n, args.project,
                                                                                                 path))
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="devtime slot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("run")
    q.add_argument("resource", help="resource, res1+res2, or a group")
    q.add_argument("--exclusive", action="store_true", help="every slot of the resource(s)")
    q.add_argument("--max-hold", type=float, help="minutes; 0 = no limit")
    q.add_argument("--label")
    q.add_argument("--shell", choices=("bash", "powershell", "pwsh"))
    q.add_argument("command", nargs="*", help="after --")
    q = sub.add_parser("status")
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("kill")
    q.add_argument("ticket")
    q = sub.add_parser("log")
    q.add_argument("--since", help="2h, 1d, 30m or an ISO time (default 1d)")
    q.add_argument("--tail", type=int, default=40)
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("rules")
    q.add_argument("--project")
    q = sub.add_parser("test")
    q.add_argument("command_text")
    q.add_argument("--tool", default="Bash")
    q.add_argument("--cwd")
    q = sub.add_parser("add-rule")
    q.add_argument("--project", required=True)
    q.add_argument("--match", required=True)
    q.add_argument("--resource", required=True)
    q.add_argument("--cwd")
    q.add_argument("--exclusive", action="store_true")
    q.add_argument("--max-hold", type=float)
    q = sub.add_parser("remove-rules")
    q.add_argument("--project", required=True)
    argv = list(argv)
    tail = []
    if argv[:1] == ["run"] and "--" in argv:  # everything after -- is the command, options included
        cut = argv.index("--")
        argv, tail = argv[:cut], argv[cut + 1:]
    args = p.parse_args(argv)
    if args.cmd == "run":
        args.command = list(args.command) + tail
        return run(args)
    return {"status": status, "kill": kill, "log": log_cmd, "rules": rules_cmd, "test": test_cmd,
            "add-rule": add_rule, "remove-rules": remove_rules}[args.cmd](args)


# ---------------------------------------------------------------------------------------------------- dashboard

def _public(t, org):
    """A ticket or log row as one org's dashboard may show it: another org's labels and paths are hidden."""
    t = dict(t)
    if org and t.get("org") and t["org"] != org:
        t.update(label="(another organisation)", cwd=None, project=None, session=None)
    return t


def summary(now=None, hours=24, org=None, cfg=None):
    """For /api/slots: limits, current holders and queue, and minutes waited / held per resource per hour (waits are
    counted in the hour the ticket got its slot; a ticket still waiting counts in the current hour)."""
    now = now or time.time()
    cfg = cfg or slotrules.Config()
    queue = Queue(cfg)
    try:
        tickets = queue.snapshot()
    except OSError:
        tickets = []
    first = int((now - hours * 3600) // 3600) * 3600
    starts = list(range(first, int(now // 3600) * 3600 + 1, 3600))
    waits, holds, counts = {}, {}, {"timeout": 0, "kill": 0, "gone": 0, "swept": 0}

    def add(table, res, t, secs):
        k = int((t - first) // 3600)
        if 0 <= k < len(starts) and secs:
            for r in res.split("+"):
                table.setdefault(r, [0.0] * len(starts))[k] += secs / 60
    rows = queue.read_log(first)
    for r in rows:
        if r["event"] in counts:
            counts[r["event"]] += 1
        if r["event"] in ("acquire", "gone") or (r["event"] == "kill" and r.get("state") == "waiting"):
            add(waits, r.get("resource") or "", r["t"], r.get("waited_s") or 0)
        if r["event"] in ("release", "timeout", "kill") and r.get("held_s") is not None:
            add(holds, r.get("resource") or "", r["t"], r.get("held_s") or 0)
    for t in tickets:
        if t.get("state") == "waiting":
            add(waits, "+".join(t.get("need") or {}), now, now - t.get("queued", now))
    names = list(cfg.resources) + sorted({r for t in tickets for r in t.get("need", {})} - set(cfg.resources))
    pub = [_public(t, org) for t in tickets]
    return {"now": now, "dir": queue.dir, "hours": starts, "limits": {n: cfg.limit(n) for n in names},
            "groups": cfg.groups, "holders": [t for t in pub if t.get("state") == "holding"],
            "queue": [t for t in pub if t.get("state") == "waiting"],
            "waits": {k: [round(x, 1) for x in v] for k, v in waits.items()},
            "holds": {k: [round(x, 1) for x in v] for k, v in holds.items()}, "counts": counts,
            "recent": [_public(r, org) for r in rows[-20:]], "alertWaitMin": cfg.settings.get("alert_wait_min", 20)}


def wait_alerts(now, org, cfg=None):
    """Usage alerts (alerts.py format) for this org's tickets that have waited longer than alert_wait_min."""
    if not org:
        return []
    cfg = cfg or slotrules.Config()
    limit = float(cfg.settings.get("alert_wait_min", 20)) * 60
    try:
        tickets = Queue(cfg).snapshot()
    except OSError:
        return []
    out = []
    for t in tickets:
        waited = now - t.get("queued", now)
        if t.get("state") != "waiting" or t.get("org") != org or waited < limit:
            continue
        res = "+".join(sorted(t.get("need") or {}))
        holders = [h for h in tickets if h.get("state") == "holding" and set(h.get("need", {})) & set(t["need"])]
        held = ", ".join("#%s '%s' for %s" % (h["id"], _public(h, org).get("label"), fmt_s(now - h.get("acquired", now)))
                         for h in holders) or "nobody (it is behind other waiters)"
        out.append({"key": "slot:%s:wait" % t["id"], "kind": "queue wait", "level": 1, "value": round(waited / 60, 1),
                    "org": org, "project": t.get("project"), "session": t.get("session"), "agent": None,
                    "owner": t.get("label") or "job", "model": "", "role": "main",
                    "title": "slot ticket #%s '%s': queue wait" % (t["id"], t.get("label")),
                    "text": "slot ticket #%s ('%s') has waited %s for '%s', held by %s" % (
                        t["id"], t.get("label"), fmt_s(waited), res, held),
                    "advice": "fine if the job must wait its turn; otherwise do other work meanwhile, check the holder "
                              "with `slot status` (a stuck holder: `slot kill <ticket>`), or raise the resource's "
                              "limit in slots.toml if the machine can take more", "since": now})
    return out
