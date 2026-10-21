"""The slot watcher: learns slot rules for the project scripts that start heavy programs, so nobody has to write them.

The hook (slotrules.hook_check) only sees the command a session gives the Bash / PowerShell tool. A project script that
starts UnrealEditor-Cmd or `blender -b` itself (`bash Tools/Dev/capture_crowd.sh`) is not queued until a rule names
the script. The watcher, run by the devtime server in a background thread (one machine-wide: an OS lock on
<slots dir>/watcher.lock; other servers skip it), lists processes every `watch_s` seconds (Windows: one
CreateToolhelp32Snapshot, pid / parent / exe name only; Linux: /proc; elsewhere `ps`). Only when a process from the
[[watch]] list appears does it read command lines, of that process and its ancestors (Windows: NtQueryInformationProcess
ProcessCommandLineInformation; the working folder from the process parameters in its PEB), and:

- skips it when an ancestor is a `slot run` wrapper (already queued), another watched program (the editor's own UBT
  run), or when no Claude Code shell (bash / sh / pwsh / powershell / cmd whose parent is claude / node / bun) is among
  its ancestors (a run the user started by hand);
- takes the outermost project script between that shell and the heavy process (an interpreter's script argument, a
  .bat / .sh run directly), or, for scripts run inside the shell's own process (a .ps1 under PowerShell, `source x.sh`),
  the one script named in the shell's command; nothing usable (an inline loop, git, make, a temp-folder script, two
  candidate scripts) is logged as "unlearnable" with the chain of process names;
- writes a rule matching the script's path inside the project (`(^|[\\\\/ '"])Tools[\\\\/]Dev[\\\\/]capture_crowd\\.sh(?![\\w.-])`)
  with the watch entry's resource ("quiet" when the script or the heavy command line matches `quiet_pattern`), the
  project (the git root's name of the shell's folder), learned = true and first / last seen, into
  <data dir>/config/slots_learned.toml, unless a rule in force already covers the script: user rules win, a learned
  rule that covers it is only refreshed (last seen, count, resources).

Privacy: nothing but process names, script paths and the project name is stored or logged; never command arguments.
"""

import json
import os
import re
import sys
import time
from struct import error as struct_error

from . import slotrules

NT = os.name == "nt"
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}
CLAUDE = re.compile(r"^(claude|node|bun)(\.exe)?$", re.IGNORECASE)
SCRIPT_EXT = (".sh", ".bash", ".zsh", ".ps1", ".psm1", ".py", ".pyw", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts",
              ".rb", ".pl")
GENERIC = {"bash", "sh", "zsh", "dash", "ksh", "python", "python3", "py", "pythonw", "pwsh", "powershell", "cmd", "node",
           "bun", "deno", "perl", "ruby", "git", "make", "mingw32-make", "nmake", "cmake", "ctest", "ninja", "msbuild",
           "dotnet", "npm", "npx", "yarn", "pnpm", "uv", "uvx", "poetry", "pipenv", "pip", "pytest", "tox", "nox", "env",
           "timeout", "nohup", "xargs", "time", "nice", "stdbuf", "winpty", "conhost", "sudo", "cargo", "go", "java",
           "gradle", "gradlew", "mvn", "start", "wsl", "claude"}
TEMP = re.compile(r"(^|/)(tmp|temp|scratchpad|shell-snapshots)(/|$)|/appdata/local/temp/", re.IGNORECASE)


def base(path):
    b = re.split(r"[\\/]", (path or "").strip().strip("'\""))[-1].lower()
    return b[:-4] if b.endswith(".exe") else b


def stem(path):
    b = base(path)
    for ext in SCRIPT_EXT:
        if b.endswith(ext):
            return b[:-len(ext)]
    return b


def split_cmdline(s):
    """argv of a Windows command line (CommandLineToArgvW rules)."""
    args, i, n = [], 0, len(s or "")
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            return args
        cur, inq = [], False
        while i < n and (inq or s[i] not in " \t"):
            c = s[i]
            if c == "\\":
                j = i
                while j < n and s[j] == "\\":
                    j += 1
                if j < n and s[j] == '"':
                    cur.append("\\" * ((j - i) // 2))
                    if (j - i) % 2:
                        cur.append('"')
                        j += 1
                    i = j
                else:
                    cur.append(s[i:j])
                    i = j
                continue
            if c == '"':
                if inq and i + 1 < n and s[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                inq = not inq
                i += 1
                continue
            cur.append(c)
            i += 1
        args.append("".join(cur))


def classify(argv):
    """What a process runs: ("script", path, index) | ("inline", text, ps) | ("program", path, 0) | None."""
    if not argv:
        return None
    prog, a = base(argv[0]), argv[1:]
    interp = "python" if re.match(r"^python(\d+(\.\d+)?)?w?$", prog) else prog
    takes = slotrules.OPTION_VALUES.get(interp, ())
    if interp in ("bash", "sh", "zsh", "dash", "ksh"):
        inline, i = False, 0
        while i < len(a) and a[i][:1] in ("-", "+") and a[i] not in ("-", "--"):
            if not a[i].startswith("--") and "c" in a[i][1:]:
                inline = True
            i += 2 if a[i].lower() in takes else 1
        if i < len(a) and a[i] == "--":
            i += 1
        if i >= len(a):
            return None
        return ("inline", a[i], False) if inline else ("script", a[i], i + 2)
    if interp in ("pwsh", "powershell"):
        i = 0
        while i < len(a):
            o = a[i].lower()
            if not o.startswith("-"):
                return ("script", a[i], i + 2) if o.endswith((".ps1", ".psm1")) else ("inline", " ".join(a[i:]), True)
            if o in ("-file", "-f"):
                return ("script", a[i + 1], i + 3) if i + 1 < len(a) else None
            if o in ("-command", "-c"):
                return ("inline", " ".join(a[i + 1:]), True)
            if o in slotrules.ENCODED and i + 1 < len(a):
                return ("inline", slotrules.decode_ps(a[i + 1]), True)
            i += 2 if o in takes else 1
        return None
    if interp == "cmd":
        for i, o in enumerate(a):
            if o.lower() in ("/c", "/k"):
                return ("inline", " ".join(a[i + 1:]), True)  # cmd syntax: backslashes are not escapes
        return None
    if interp in ("python", "py", "node", "perl", "ruby", "bun", "deno"):
        i = 0
        while i < len(a) and a[i].startswith("-"):
            if a[i] in ("-c", "-e", "-m", "--eval"):
                return None  # inline code or a module: no script file
            i += 2 if a[i].lower() in takes else 1
        if interp in ("bun", "deno") and i < len(a) and a[i] == "run":
            i += 1
        return ("script", a[i], i + 2) if i < len(a) else None
    return ("program", argv[0], 1)


# --------------------------------------------------------------------------------------------- process tables

class StaticTable:
    """A fixed process table (tests, `slot watch --table FILE`): rows of {pid, ppid, name, cmdline or argv, cwd,
    start}."""

    def __init__(self, rows):
        self.rows = {int(r["pid"]): r for r in rows}

    def procs(self):
        return {pid: (int(r.get("ppid") or 0), r.get("name") or base(self._argv(r)[0] if self._argv(r) else ""))
                for pid, r in self.rows.items()}

    @staticmethod
    def _argv(r):
        if r.get("argv") is not None:
            return list(r["argv"])
        return split_cmdline(r.get("cmdline") or "")

    def argv(self, pid):
        r = self.rows.get(pid)
        return self._argv(r) if r else None

    def cwd(self, pid):
        return (self.rows.get(pid) or {}).get("cwd")

    def start(self, pid):
        return (self.rows.get(pid) or {}).get("start")


if NT:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")

    class _ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    class _USTR(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    class _PBI(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_long), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_size_t), ("BasePriority", ctypes.c_long),
                    ("UniqueProcessId", ctypes.c_size_t), ("InheritedFrom", ctypes.c_size_t)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ENTRY))
    _k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ENTRY))
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _k32.ReadProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t))
    _k32.IsWow64Process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _nt.NtQueryInformationProcess.restype = ctypes.c_long
    _nt.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                              ctypes.POINTER(ctypes.c_ulong))
    _nt.NtQuerySystemInformation.restype = ctypes.c_long
    _nt.NtQuerySystemInformation.argtypes = (ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.POINTER(ctypes.c_ulong))
    _INVALID = wintypes.HANDLE(-1).value
    _LIMITED, _QUERY, _VM_READ = 0x1000, 0x0400, 0x0010


class WinTable:
    """Windows: one NtQuerySystemInformation(SystemProcessInformation) call for the list (pid, parent, image name,
    creation time; ~8 ms for 450 processes, half a Toolhelp snapshot's cost), Toolhelp as the fallback; command line
    and folder only on demand."""

    def __init__(self):
        self._buf, self._starts = None, {}

    def procs(self):
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            try:
                out = self._ntprocs()
                if out:
                    return out
            except (OSError, ValueError, struct_error):
                pass
        self._starts = {}
        return self._toolhelp()

    def _ntprocs(self):
        import struct
        size = len(self._buf) if self._buf is not None else 1 << 21
        for _ in range(5):
            if self._buf is None or len(self._buf) < size:
                self._buf = ctypes.create_string_buffer(size)
            got = ctypes.c_ulong(0)
            st = _nt.NtQuerySystemInformation(5, self._buf, len(self._buf), ctypes.byref(got)) & 0xFFFFFFFF
            if st == 0:
                break
            if st != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH: grow and retry
                return None
            size = max(len(self._buf) * 2, got.value + (1 << 16))
            self._buf = None
        else:
            return None
        buf, start_addr, off, out, starts = self._buf, ctypes.addressof(self._buf), 0, {}, {}
        while True:
            nxt = struct.unpack_from("<I", buf, off)[0]
            created = struct.unpack_from("<q", buf, off + 0x20)[0]
            length, ptr = struct.unpack_from("<H6xQ", buf, off + 0x38)
            pid, ppid = struct.unpack_from("<QQ", buf, off + 0x50)
            if ptr and length:
                name = buf[ptr - start_addr:ptr - start_addr + length].decode("utf-16-le", "replace")
            else:
                name = "System" if pid else "Idle"
            out[int(pid)] = (int(ppid), name)
            starts[int(pid)] = created / 1e7 - 11644473600.0 if created else None
            if not nxt:
                break
            off += nxt
        self._starts = starts
        return out

    def _toolhelp(self):
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == _INVALID:
            return {}
        out, e = {}, _ENTRY()
        e.dwSize = ctypes.sizeof(_ENTRY)
        try:
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile)
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out

    @staticmethod
    def _cmdline(pid):
        h = _k32.OpenProcess(_LIMITED, False, pid)
        if not h:
            return None
        try:
            size = 4096
            for _ in range(3):
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_ulong(0)
                st = _nt.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(got))  # ProcessCommandLineInformation
                if st == 0:
                    us = _USTR.from_buffer(buf)
                    return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
                if got.value <= size:
                    return None
                size = got.value + 64
            return None
        finally:
            _k32.CloseHandle(h)

    def argv(self, pid):
        text = self._cmdline(pid)
        return None if text is None else split_cmdline(text)

    def cwd(self, pid):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            return None
        h = _k32.OpenProcess(_QUERY | _VM_READ, False, pid)
        if not h:
            return None
        try:
            wow = wintypes.BOOL(0)
            if _k32.IsWow64Process(h, ctypes.byref(wow)) and wow.value:
                return None  # a 32-bit process: other offsets
            pbi, got = _PBI(), ctypes.c_ulong(0)
            if _nt.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(got)) != 0:
                return None

            def read(addr, size):
                buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t(0)
                if not addr or not _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
                    return None
                return buf.raw[:n.value]
            raw = read(pbi.PebBaseAddress, 0x28)  # PEB.ProcessParameters at 0x20 (x64)
            if not raw or len(raw) < 0x28:
                return None
            params = int.from_bytes(raw[0x20:0x28], "little")
            raw = read(params + 0x38, 16)  # RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath (x64)
            if not raw or len(raw) < 16:
                return None
            length, addr = int.from_bytes(raw[0:2], "little"), int.from_bytes(raw[8:16], "little")
            text = read(addr, length) if length else b""
            return text.decode("utf-16-le", "replace") if text is not None else None
        finally:
            _k32.CloseHandle(h)

    def start(self, pid):
        if pid in self._starts:  # from the last list
            return self._starts[pid]
        h = _k32.OpenProcess(_LIMITED, False, pid)
        if not h:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
                return None
            return ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 1e7 - 11644473600.0
        finally:
            _k32.CloseHandle(h)


class ProcTable:
    """Linux: /proc."""

    def procs(self):
        out = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d, encoding="utf-8", errors="replace") as f:
                    stat = f.read()
            except OSError:
                continue
            name = stat[stat.find("(") + 1:stat.rfind(")")]
            fields = stat[stat.rfind(")") + 2:].split()
            if len(name) >= 15:  # comm is cut at 15 characters: the program's own name instead
                argv = self.argv(int(d))
                name = os.path.basename(argv[0]) if argv else name
            out[int(d)] = (int(fields[1]) if len(fields) > 1 else 0, name)
        return out

    def argv(self, pid):
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
        except OSError:
            return None

    def cwd(self, pid):
        try:
            return os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            return None

    def start(self, pid):
        try:
            with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as f:
                stat = f.read()
            return float(stat[stat.rfind(")") + 2:].split()[19])  # starttime, in clock ticks since boot
        except (OSError, IndexError, ValueError):
            return None


class PsTable:
    """Other POSIX systems: `ps` (one call per check)."""

    def procs(self):
        import subprocess
        try:
            text = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True,
                                  timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        out = {}
        for line in text.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                out[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
        return out

    def argv(self, pid):
        import shlex
        import subprocess
        try:
            text = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
            return shlex.split(text) if text else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def cwd(self, pid):
        return None

    def start(self, pid):
        return None


def default_table():
    if NT:
        return WinTable()
    return ProcTable() if os.path.isdir("/proc/self") else PsTable()


# --------------------------------------------------------------------------------------------------- analysis

def to_native(path):
    """/d/dev/x -> d:/dev/x on Windows; backslashes to slashes."""
    p = (path or "").strip().strip("'\"").replace("\\", "/")
    if NT:
        m = re.match(r"^/([a-zA-Z])(/.*|$)", p)
        if m:
            p = "%s:%s" % (m.group(1), m.group(2) or "/")
    return p


def resolve(path, cwd):
    p = to_native(path)
    if not os.path.isabs(p) and cwd:
        p = os.path.join(to_native(cwd), p)
    return os.path.normpath(p).replace("\\", "/") if os.path.isabs(p) else None


def project_root(folder):
    """(root, name): the git work tree holding the folder and its project name (a linked worktree is named after its
    main repository), or (None, None)."""
    if not folder:
        return None, None
    d = os.path.abspath(to_native(folder))
    while True:
        git = os.path.join(d, ".git")
        if os.path.isdir(git):
            return d.replace("\\", "/"), os.path.basename(d) or d
        if os.path.isfile(git):
            name = os.path.basename(d)
            try:
                with open(git, encoding="utf-8") as f:
                    m = re.match(r"gitdir:\s*(.+)", f.read().strip())
                if m:
                    main = re.split(r"[\\/]\.git[\\/]worktrees[\\/]", m.group(1).strip())[0]
                    if main != m.group(1).strip():
                        name = os.path.basename(main.rstrip("\\/")) or name
            except OSError:
                pass
            return d.replace("\\", "/"), name
        up = os.path.dirname(d)
        if up == d:
            return None, None
        d = up


def inside(path, root):
    if not (path and root):
        return False
    p, r = os.path.normcase(path).replace("\\", "/"), os.path.normcase(root).replace("\\", "/").rstrip("/")
    return p == r or p.startswith(r + "/")


def rule_regex(tail):
    parts = [re.escape(c) for c in tail.split("/") if c]
    rx = r"(^|[\\/ '" + '"' + r"])" + r"[\\/]".join(parts)
    return rx + r"(?![\w.-])"  # not x.sh.bak / x.sh-old


def _inline_scripts(text, ps):
    """Script files named as programs in a command line (incl. `source x.sh` / `. x.ps1`)."""
    out = []
    for cand, plen, _sep, _async in slotrules.programs(text or "", ps):
        word = cand[:plen].strip().strip("'\"")
        if base(word) in ("source", "."):
            rest = slotrules._words(cand[plen:], ps)
            word = rest[0][2] if rest else ""
        if word.lower().endswith(SCRIPT_EXT) and word not in out:
            out.append(word)
    return out


def _quote(s):
    return '"%s"' % s if re.search(r"\s", s) else s


def analyze(pid, procs, table, cfg, entry):
    """What the watcher makes of one heavy process: {status: learn | covered | skip | unlearnable, reason, heavy, chain,
    ...} (learn: + rule spec; covered: + the rule)."""
    heavy = procs[pid][1]
    chain, cur = [pid], pid
    while len(chain) < 40:
        ppid = procs[cur][0]
        if not ppid or ppid in chain or ppid not in procs:
            break
        ps_, cs = table.start(ppid), table.start(cur)
        if ps_ is not None and cs is not None and ps_ > cs + 1:
            break  # that parent is gone and its pid reused
        chain.append(ppid)
        cur = ppid
    names = [procs[p][1] for p in chain]
    out = {"heavy": heavy, "pid": pid, "chain": names, "resource": entry["resource"]}
    for p in chain[1:]:
        if any(w["name"].search(procs[p][1]) for w in cfg.watch):
            return dict(out, status="skip", reason="under %s" % procs[p][1])
    top = None
    for i in range(1, len(chain) - 1):
        if base(procs[chain[i]][1]) in SHELLS and CLAUDE.match(procs[chain[i + 1]][1]):
            top = i
            break
    if top is None and len(chain) > 1 and procs[chain[-1]][0] not in procs and \
            (base(procs[chain[-1]][1]) in SHELLS or re.match(r"^(python[\d.]*w?|py|node)$", base(procs[chain[-1]][1]))):
        # orphaned: the shell (or interpreter) that started it outlived its parent, typically a Claude Code tool
        # shell that timed out or was stopped while its job went on; nothing in the chain is a terminal or the desktop
        top = len(chain) - 1
        out["orphan"] = True
    if top is None:
        return dict(out, status="skip", reason="not started from a Claude Code shell")
    out["chain"] = names[:top + 2]
    argvs = {p: table.argv(p) or [] for p in chain[:top + 1]}
    for p in chain[:top + 1]:
        if slotrules.WRAPPED.search(" ".join(argvs[p])):
            return dict(out, status="skip", reason="already queued (slot run)")
    if re.search(r"\bDEVTIME_SLOTS\s*=\s*['\"]?0\b", " ".join(argvs[chain[top]])):
        return dict(out, status="skip", reason="DEVTIME_SLOTS=0")
    shell_cwd = table.cwd(chain[top])
    root, project = project_root(shell_cwd)

    cands, inlines = [], []
    for i in range(top, 0, -1):  # outermost first (a Claude Code shell itself runs an inline command)
        p = chain[i]
        kind = classify(argvs[p])
        if not kind:
            continue
        if kind[0] == "inline":
            inlines.append((kind[1], kind[2], p))
            continue
        path = kind[1]
        if stem(path) in GENERIC:
            continue
        if kind[0] == "program" and not path.lower().endswith(SCRIPT_EXT):
            where = resolve(path, table.cwd(p) or shell_cwd)
            if not inside(where, root):
                continue  # a tool, not a project script
        rest = " ".join(_quote(a) for a in argvs[p][kind[2]:])
        cands.append({"path": path, "proc": p, "text": (_quote(path) + " " + rest).strip()})
    if not cands:
        found = []
        for text, ps, p in inlines:
            for word in _inline_scripts(text, ps):
                where = resolve(word, table.cwd(p) or shell_cwd) or to_native(word)
                if "shell-snapshots" in where or (TEMP.search(where) and not inside(where, root)):
                    continue
                if stem(word) not in GENERIC and word not in [f["path"] for f in found]:
                    found.append({"path": word, "proc": p, "text": _quote(word)})
        if len(found) > 1:
            return dict(out, status="unlearnable", reason="several scripts in the command",
                        scripts=[f["path"] for f in found])
        cands = found
    picked = []
    for c in cands:
        where = resolve(c["path"], table.cwd(c["proc"]) or shell_cwd)
        if root is None and where:
            root, project = project_root(os.path.dirname(where))
        if TEMP.search(where or to_native(c["path"])) and not inside(where, root):
            continue  # a throw-away script (temp folder, scratchpad, shell snapshot) outside any project
        c["where"] = where
        c["inside"] = inside(where, root) if where else not os.path.isabs(to_native(c["path"]))
        picked.append(c)
    if not picked:
        return dict(out, status="unlearnable", reason="no project script between the shell and %s" % heavy)
    best = next((c for c in picked if c["inside"]), picked[0])
    if best["where"] and root and best["inside"]:
        tail = os.path.relpath(best["where"], root).replace("\\", "/")
    else:
        parts = [x for x in to_native(best["path"]).split("/") if x and x != "."]
        tail = "/".join(parts[-2:])
    if not tail or tail.startswith(".."):
        return dict(out, status="unlearnable", reason="no usable script path")
    out.update(script=tail, project=project or "")
    tool = "PowerShell" if tail.lower().endswith((".ps1", ".psm1")) else "Bash"
    rule = slotrules.first_rule(best["text"], tool, shell_cwd or "", cfg, ps=False)
    if rule is not None:
        return dict(out, status="covered", rule=rule.match_text, learned=rule.learned,
                    reason="covered by %s rule %s" % ("a learned" if rule.learned else "the", rule.match_text))
    resource, pattern = entry["resource"], str(cfg.settings.get("quiet_pattern") or "")
    try:
        quiet = re.compile(pattern or r"(?!)")
    except re.error:
        quiet, pattern = re.compile(r"(?!)"), ""
    args_quiet = bool(quiet.search(best["text"][len(_quote(best["path"])):]))
    if quiet.search(tail) or (quiet.search(" ".join(argvs[pid][1:])) and not args_quiet):
        resource = "quiet"  # the script always times something (its name, or what it starts whatever its arguments)
    spec = {"match": rule_regex(tail), "resource": resource, "project": project or "", "learned": True,
            "script": tail, "heavy": heavy, "example": "%s under %s" % (heavy, tail)}
    if resource != "quiet" and pattern:
        spec["quiet_if"] = pattern  # the hook makes a run of it "quiet" when its command line matches (measure_x.py)
    if "/" not in tail and project:  # a script at a project's root: too common a name for every project
        spec["cwd"] = r"(^|/)" + re.escape(project.lower()) + r"(/|$)"
    return dict(out, status="learn", resource=resource, rule=spec)


# -------------------------------------------------------------------------------------------- learned rules file

HEADER = ("# Slot rules learned by the devtime slot watcher (devtime/slotwatch.py): machine-wide, rewritten by it.\n"
          "# `slot rules` lists them, `slot remove-rules --learned [--project P]` removes them. To change one, add a rule\n"
          "# for the same script to your slots.toml (resource = \"none\" exempts it): your rules are checked first.\n")
FIELDS = ("match", "resource", "project", "cwd", "exclusive", "max_hold", "learned", "script", "heavy", "example",
          "quiet_if", "first_seen", "last_seen", "seen")


def _lit(s):
    return "'''%s'''" % s if "'''" not in s and not s.endswith("'") and "\n" not in s else json.dumps(s)


def load_learned(path=None):
    path = path or slotrules.learned_file()
    try:
        data = slotrules._toml(path)
    except ValueError:
        return []
    return [r for r in data.get("rule") or [] if isinstance(r, dict) and r.get("match")]


def save_learned(rules, path=None):
    path = path or slotrules.learned_file()
    lines = [HEADER]
    for r in rules:
        lines.append("[[rule]]")
        for k in FIELDS + tuple(sorted(set(r) - set(FIELDS))):
            v = r.get(k)
            if v is None or isinstance(v, (dict, list)):
                continue
            if isinstance(v, bool):
                text = "true" if v else "false"
            elif isinstance(v, (int, float)):
                text = repr(v)
            else:
                text = _lit(str(v)) if k in ("match", "cwd", "quiet_if") else json.dumps(str(v))
            lines.append("%s = %s" % (k, text))
        lines.append("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


def _merge_resource(old, new):
    if "quiet" in (old, new):
        return "quiet"
    parts = [p for p in str(old).split("+") if p]
    return "+".join(parts + [p for p in str(new).split("+") if p and p not in parts])


def remember(result, cfg):
    """Writes a learn / learned-covered result into the learned file. Returns "added", "updated" or None."""
    path = cfg.learned_path or slotrules.learned_file()
    rules, now = load_learned(path), time.strftime("%Y-%m-%d %H:%M:%S")
    if result["status"] == "covered":
        for r in rules:
            if r.get("match") == result["rule"]:
                r.update(last_seen=now, seen=int(r.get("seen") or 1) + 1,
                         resource=_merge_resource(r.get("resource") or "", result["resource"]))
                save_learned(rules, path)
                return "updated"
        return None
    spec = dict(result["rule"], first_seen=now, last_seen=now, seen=1)
    rules = [r for r in rules if r.get("match") != spec["match"]] + [spec]
    cap = max(1, int(cfg.settings.get("learn_max", 100) or 100))
    if len(rules) > cap:
        rules = sorted(rules, key=lambda r: str(r.get("last_seen") or ""))[-cap:]
    save_learned(rules, path)
    return "added"


def remove_learned(project=None, path=None):
    rules = load_learned(path)
    keep = [r for r in rules if project and str(r.get("project") or "").lower() != project.lower()]
    if len(keep) != len(rules):
        save_learned(keep, path)
    return len(rules) - len(keep)


# ------------------------------------------------------------------------------------------------------ watcher

def learning_on(cfg):
    v = cfg.settings.get("learn", True)
    return v if isinstance(v, bool) else str(v).strip().lower() not in ("0", "false", "no", "off", "")


class Watcher:
    def __init__(self, table=None, say=None):
        self.table = table or default_table()
        self.say = say or (lambda *a: None)
        self.done = set()   # (pid, start) of watched processes already looked at
        self.noted = {}     # chain signature -> time an "unlearnable" was last logged

    def scan(self, cfg=None, dry=False):
        """One check: [results] for the watched processes that appeared since the last one."""
        cfg = cfg or slotrules.config()
        if not learning_on(cfg) or not cfg.watch:
            return []
        procs = self.table.procs()
        results, keep = [], set()
        for pid, (_ppid, name) in procs.items():
            entries = [w for w in cfg.watch if w["name"].search(name or "")]
            if not entries:
                continue
            key = (pid, self.table.start(pid))
            keep.add(key)
            if key in self.done:
                continue
            self.done.add(key)
            line = None  # the first entry whose args (if any) the command line has: e.g. -nullrhi -> test, else gpu
            for entry in entries:
                if entry["args"]:
                    if line is None:
                        line = " " + " ".join((self.table.argv(pid) or [])[1:])
                    if not entry["args"].search(line):
                        continue
                break
            else:
                continue
            if str(entry["resource"]).lower() == "none":  # e.g. an interactive editor: nothing to learn
                continue
            try:
                r = analyze(pid, procs, self.table, cfg, entry)
            except (OSError, KeyError, ValueError, IndexError) as e:
                r = {"status": "skip", "heavy": name, "pid": pid, "reason": "error %r" % e}
            if not dry:
                self.record(r, cfg)
            results.append(r)
        self.done &= keep
        return results

    def record(self, r, cfg):
        from . import slots
        queue = slots.Queue(cfg)
        info = {"need": {x: 1 for x in str(r.get("resource") or "").split("+") if x}, "label": r.get("script"),
                "project": r.get("project")}
        try:
            os.makedirs(queue.dir, exist_ok=True)
        except OSError:
            return
        if r["status"] == "learn":
            if remember(r, cfg) == "added":
                queue.log("learned", info, rule=r["rule"]["match"], script=r["script"], heavy=r["heavy"])
                self.say("slot watcher: learned %s -> %s (%s, started %s)" % (r["script"], r["resource"],
                                                                          r.get("project") or "?", r["heavy"]))
        elif r["status"] == "covered" and r.get("learned"):
            remember(r, cfg)
        elif r["status"] == "unlearnable":
            sig = "/".join(r.get("chain") or []) + "|" + r.get("reason", "")
            if time.time() - self.noted.get(sig, 0) > 3600:
                self.noted[sig] = time.time()
                queue.log("unlearnable", dict(info, label=r.get("heavy")), chain=r.get("chain"),
                          reason=r.get("reason"), scripts=r.get("scripts"))
                self.say("slot watcher: can't learn a rule for %s (%s; %s)" % (r["heavy"], r.get("reason"),
                                                                              " < ".join(r.get("chain") or [])))


def take_lock(directory):
    """An OS lock on <dir>/watcher.lock (held while the returned fd stays open), or None if another process has it."""
    from . import slots
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, "watcher.lock"), os.O_RDWR | os.O_CREAT | slots.O_BIN, 0o666)
    except OSError:
        return None
    if slots.try_lock(fd):
        return fd
    os.close(fd)
    return None


def run_forever(say=None, stop=None, table=None):
    """The server's watcher thread: takes the machine-wide watcher lock (retried every minute while another process
    holds it) and checks every watch_s seconds."""
    wait = stop.wait if stop else time.sleep
    fd, watcher = None, None
    while not (stop and stop.is_set()):
        cfg = slotrules.config()
        if fd is None:
            fd = take_lock(cfg.lock_dir())
            if fd is None:
                wait(60)
                continue
            watcher = Watcher(table, say)
        try:
            watcher.scan(cfg)
        except Exception as e:  # never let the watcher take the server down
            if say:
                say("slot watcher: check failed:", repr(e))
        wait(max(1.0, float(cfg.settings.get("watch_s", 5) or 5)))
    if fd is not None:
        os.close(fd)


def watch_cmd(args):
    """`slot watch [--once] [--dry-run]`: the watcher in the foreground (normally the devtime server runs it)."""
    cfg = slotrules.config()
    if args.once:
        table = default_table()
        if args.table:
            with open(args.table, encoding="utf-8") as f:
                table = StaticTable(json.load(f))
        t0 = time.perf_counter()
        results = Watcher(table, print).scan(cfg, dry=args.dry_run)
        for r in results:
            print(json.dumps({k: v for k, v in r.items() if k != "pid"}))
        print("checked in %.1f ms; %d watched process(es) looked at%s" % ((time.perf_counter() - t0) * 1000,
                                                                          len(results),
                                                                          "" if learning_on(cfg) else " (learn = false)"))
        return 0
    fd = take_lock(cfg.lock_dir())
    if fd is None:
        print("another process (a devtime server) is already the slot watcher")
        return 1
    os.close(fd)
    try:
        run_forever(lambda *a: print(*a, flush=True))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit("run it as: python -m devtime slot watch")
