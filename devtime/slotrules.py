"""Slot configuration and command rules: which shell commands queue for which machine-wide slot (devtime/slots.py).

Light on purpose: hooks/agent_budget.py imports it on every Bash / PowerShell call (stdlib only, no subprocess).

Configuration: <plugin>/config/slots.toml (shipped defaults) under <data dir>/config/slots.toml (the user's, shared by
every project and org on the PC): [settings], [resources] and [groups] merge key by key, the user's [[rule]]s are checked
before the shipped ones.

A rule's `match` is a regular expression (case-insensitive) searched in each simple command of a command line (split at
; && || | & newlines, brackets and $( ); it counts only if the match starts inside the program the command runs: the
first word after env assignments and wrappers (env, nohup, timeout N, cmd /c, Start-Process ...), or, for an
interpreter (bash, sh, python, pwsh, node ...), the script it is given (its -c / -Command string is checked like a
command line). So `grep UnrealEditor log.txt`, `tasklist | findstr UnrealEditor` or `cat Build.bat` never queue, and
`"$UE/Engine/Binaries/Win64/UnrealEditor-Cmd.exe" ...`, `& $editor ...` (after `$editor = "...UnrealEditor-Cmd.exe"`) or
`bash Tools/Dev/capture_x.sh` do. resource = "none" exempts what it matches (the first matching rule of a command wins).
"""

import os
import re
import sys

from . import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = os.path.join(ROOT, "config", "slots.toml")
DEFAULT_SETTINGS = {"dir": "", "max_hold": 45, "poll_s": 1.0, "log_kb": 512, "alert_wait_min": 20, "default_limit": 1,
                    "learn": True, "learn_max": 100, "watch_s": 5,
                    "quiet_pattern": r"(?i)(?<![a-z])slow(?![a-z])|perf|timing|benchmark|measure"}
DEFAULT_RESOURCES = {"gpu": 2, "test": 3, "build": 2}
DEFAULT_GROUPS = {"quiet": ["test", "gpu"]}


def user_file():
    return os.path.join(paths.USER_CONFIG, "slots.toml")


def learned_file():
    """Rules the slot watcher learned (devtime/slotwatch.py): machine-wide, never the user's slots.toml."""
    return os.path.join(paths.USER_CONFIG, "slots_learned.toml")


def enabled(command=""):
    """False when the escape hatch is set: env DEVTIME_SLOTS=0 (the session's), or DEVTIME_SLOTS=0 in the command."""
    if os.environ.get("DEVTIME_SLOTS", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return not re.search(r"\bDEVTIME_SLOTS\s*=\s*['\"]?0\b", command or "")


class Rule:
    def __init__(self, spec, source):
        self.spec = spec
        self.source = source
        self.match_text = str(spec.get("match") or "")
        self.rx = re.compile(self.match_text, re.IGNORECASE)
        self.resource = str(spec.get("resource") or "none").strip()
        self.exclusive = bool(spec.get("exclusive"))
        self.max_hold = spec.get("max_hold")
        self.tool = re.compile(spec["tool"]) if spec.get("tool") else None
        self.cwd = re.compile(spec["cwd"], re.IGNORECASE) if spec.get("cwd") else None
        self.project = spec.get("project")
        self.learned = bool(spec.get("learned"))
        # quiet_if: a regex; a matching command whose text (program and arguments) also matches it needs "quiet"
        # instead (e.g. the same gpu script running a timing measurement)
        self.quiet_if = re.compile(str(spec["quiet_if"]), re.IGNORECASE) if spec.get("quiet_if") else None

    def resource_for(self, text):
        if self.quiet_if and self.resource.lower() != "none" and self.quiet_if.search(text or ""):
            return "quiet"
        return self.resource

    def applies(self, tool, cwd):
        if self.tool and not self.tool.search(tool or ""):
            return False
        if self.cwd and not self.cwd.search(norm(cwd)):
            return False
        return True


def norm(path):
    """Lower-case, forward slashes, /d/... -> d:/..."""
    p = (path or "").replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^/([a-z])/(.*)", p)
    return f"{m.group(1)}:/{m.group(2)}" if m else p


def _toml(path):
    import tomllib
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _resources(table):
    out = {}
    for name, value in (table or {}).items():
        if isinstance(value, dict):
            out[name] = {"limit": int(value.get("limit", 1)), "max_hold": value.get("max_hold")}
        else:
            out[name] = {"limit": int(value), "max_hold": None}
    return out


class Config:
    """The merged slot configuration."""

    def __init__(self, shipped=SHIPPED, user=None, learned=None):
        user = user_file() if user is None else user
        learned = learned_file() if learned is None else learned
        self.learned_path = learned
        base, mine = _toml(shipped), _toml(user) if user else {}
        try:
            taught = _toml(learned) if learned else {}
        except ValueError:  # a damaged learned file costs the learned rules, never the config
            taught = {}
        self.settings = {**DEFAULT_SETTINGS, **(base.get("settings") or {}), **(mine.get("settings") or {})}
        res = _resources(DEFAULT_RESOURCES)
        for layer in (base, mine):
            for name, spec in _resources(layer.get("resources")).items():
                res[name] = dict(res.get(name, {}), **{k: v for k, v in spec.items() if v is not None})
        self.resources = res
        self.groups = {**DEFAULT_GROUPS, **(base.get("groups") or {}), **(mine.get("groups") or {})}
        self.rules = []  # user rules first (they win), then learned ones, then the shipped defaults
        for layer, source in ((mine, user), (taught, learned), (base, shipped)):
            for spec in layer.get("rule") or []:
                try:
                    rule = Rule(spec, source)
                except (re.error, TypeError, KeyError, AttributeError):
                    continue  # a broken rule is skipped, never the whole config
                if (source == learned) != rule.learned:
                    rule.learned = source == learned  # only the learned file holds learned rules
                self.rules.append(rule)
        self.watch = []  # [[watch]]: heavy programs the watcher learns launchers for (user entries first)
        for layer in (mine, base):
            for spec in layer.get("watch") or []:
                try:
                    self.watch.append({"name": re.compile(str(spec["name"]), re.IGNORECASE),
                                       "args": re.compile(str(spec["args"]), re.IGNORECASE) if spec.get("args") else None,
                                       "resource": str(spec.get("resource") or "test")})
                except (re.error, TypeError, KeyError):
                    continue

    def limit(self, name):
        spec = self.resources.get(name)
        return max(1, int(spec["limit"])) if spec else max(1, int(self.settings.get("default_limit", 1)))

    def max_hold_min(self, names):
        """Minutes a holder of these resources may run (the largest of theirs; 0 = no limit)."""
        values = []
        for n in names:
            v = (self.resources.get(n) or {}).get("max_hold")
            values.append(float(self.settings.get("max_hold", 45) if v is None else v))
        return max(values) if values else float(self.settings.get("max_hold", 45))

    def expand(self, spec, exclusive=False):
        """{resource: need} of a request like "test", "test+gpu" or a group ("quiet": exclusive over its resources).
        need -1 = every slot of the resource."""
        need = {}
        for part in filter(None, (p.strip() for p in str(spec).split("+"))):
            if part in self.groups:
                for r in self.groups[part]:
                    need[r] = -1
            else:
                need[part] = -1 if exclusive else max(need.get(part, 0), 1)
        return need

    def lock_dir(self):
        d = os.environ.get("DEVTIME_SLOTS_DIR") or self.settings.get("dir") or ""
        if d:
            return os.path.abspath(os.path.expanduser(d))
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            return os.path.join(os.environ["LOCALAPPDATA"], "devtime-slots")
        return os.path.join(paths.DATA, "slots")


# ------------------------------------------------------------------------------------------------ command analysis

WRAPPERS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time", "exec", "nohup", "env", "command",
            "builtin", "sudo", "nice", "ionice", "stdbuf", "call", "xargs", "uv", "run", "npx", "poetry", "pipenv",
            "start", "start-process", "saps", "invoke-expression", "iex", "eval", "timeout", "cmd", "not"}
INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3", "py", "pwsh", "powershell", "node", "perl", "ruby",
                "wsl", "cmd"}
INLINE = {"-c", "-command", "/c", "/k", "eval", "invoke-expression", "iex"}
# options whose value is the next word (so the value is not taken for the script), per interpreter (lower case)
_PS_VALUES = {"-executionpolicy", "-ep", "-ex", "-exec", "-windowstyle", "-w", "-win", "-version", "-v",
              "-configurationname", "-config", "-inputformat", "-if", "-inp", "-outputformat", "-of", "-o",
              "-workingdirectory", "-wd", "-psconsolefile", "-settingsfile", "-settings", "-custompipename",
              "-encodedarguments", "-ea"}
_PY_VALUES = {"-x", "-w", "-q"}
OPTION_VALUES = {"powershell": _PS_VALUES, "pwsh": _PS_VALUES, "bash": {"-o", "+o", "--rcfile", "--init-file"},
                 "sh": {"-o", "+o"}, "zsh": {"-o", "+o"}, "dash": {"-o", "+o"}, "python": _PY_VALUES,
                 "python3": _PY_VALUES, "py": _PY_VALUES, "node": {"-r", "--require", "--import", "--loader"},
                 "perl": {"-i", "-m"}, "ruby": {"-r", "-i"}}
CODE_INLINE = {"python": {"-c"}, "python3": {"-c"}, "py": {"-c"}, "node": {"-e", "--eval", "-p", "--print"},
               "perl": {"-e", "-E"}, "ruby": {"-e"}}
SCRIPT_NEXT = {"powershell": {"-file", "-f"}, "pwsh": {"-file", "-f"}}
ENCODED = {"-encodedcommand", "-enc", "-e", "-ec", "-en", "-encoded"}


def decode_ps(text):
    """The command of powershell -EncodedCommand (base64 of UTF-16LE), or '' if it doesn't decode."""
    import base64
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-16-le")
    except (ValueError, UnicodeDecodeError):
        return ""
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VARREF = re.compile(r"^\$(?:env:)?\{?([A-Za-z_][A-Za-z0-9_]*)\}?", re.IGNORECASE)


HEREDOC = re.compile(r"""<<(-?)\s*(['"]?)([A-Za-z_][A-Za-z0-9_.\-]*)\2""")  # not $((1 << 2))


def _segments(text, ps):
    """[(start, end, separator after)] of the simple commands, split at unquoted separators."""
    out, start, i, n, q, heredocs = [], 0, 0, len(text), None, []
    while i < n:
        c = text[i]
        if q:
            if c == q:
                q = None
            elif q == '"' and (c == "`" if ps else c == "\\" and text[i + 1:i + 2] in ('"', "\\", "$", "`")):
                i += 1
            i += 1
            continue
        if c in "'\"":
            q = c
            i += 1
            continue
        if (c == "\\" and not ps) or (c == "`" and ps):
            i += 2
            continue
        if c == "#" and (i == 0 or text[i - 1].isspace()):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "$" and text.startswith("${", i):
            j = text.find("}", i)
            i = n if j < 0 else j + 1
            continue
        if not ps and text.startswith("<<", i) and not text.startswith("<<<", i):
            m = HEREDOC.match(text, i)
            if m:
                heredocs.append((m.group(1) == "-", m.group(3)))
                i = m.end()
                continue
        if c == "\n" and heredocs:  # here-document bodies are data, not commands: skip to their end lines
            out.append((start, i, "\n"))
            j = i + 1
            for dash, word in heredocs:
                while j < n:
                    k = text.find("\n", j)
                    line = text[j:n if k < 0 else k]
                    j = n if k < 0 else k + 1
                    if (line.lstrip("\t") if dash else line).rstrip("\r") == word:
                        break
            heredocs = []
            i = start = j
            continue
        sep = None
        if text.startswith(("&&", "||"), i):
            sep = text[i:i + 2]
        elif text.startswith("$(", i):
            sep = "$("
        elif c in ";|\n(){}" or (c == "`" and not ps):
            sep = c
        elif c == "&" and not (i and text[i - 1] in "<>") and not text.startswith("&>", i):
            sep = "&"
        if sep:
            out.append((start, i, sep))
            i += len(sep)
            start = i
            continue
        i += 1
    out.append((start, n, ""))
    return out


def _words(text, ps):
    """[(start, end, value without quotes)] of the words of one simple command."""
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        s, q, val = i, None, []
        while i < n and (q or not text[i].isspace()):
            c = text[i]
            if q:
                if c == q:
                    q = None
                elif q == '"' and i + 1 < n and (c == "`" if ps else
                                                 c == "\\" and text[i + 1] in ('"', "\\", "$", "`")):
                    i += 1
                    val.append(text[i])
                else:
                    val.append(c)
            elif c in "'\"":
                q = c
            elif ((c == "\\" and not ps) or (c == "`" and ps)) and i + 1 < n:
                i += 1
                val.append(text[i])
            else:
                val.append(c)
            i += 1
        out.append((s, i, "".join(val)))
    return out


def _base(word):
    b = re.split(r"[\\/]", word.strip())[-1].lower()
    return b[:-4] if b.endswith(".exe") else b


def programs(text, ps=False, depth=0):
    """[(candidate text, program length, segment separator, async)]: for each simple command, the program it runs (and
    for interpreters, the script) with the rest of the command after it. A rule matches only at a position < length."""
    out, variables = [], {}
    for s, e, sep in _segments(text, ps):
        seg = text[s:e]
        words = _words(seg, ps)
        if not words:
            continue
        # assignments: NAME=value (bash, alone on the line) and $name = value (PowerShell)
        if len(words) >= 3 and words[1][2] == "=" and words[0][2].startswith("$"):
            m = VARREF.match(words[0][2])
            if m:
                variables[m.group(1).lower()] = seg[words[2][0]:].strip().strip("'\"")
            continue
        k = 0
        while k < len(words) and ASSIGN.match(words[k][2]) and not words[k][2].startswith("-"):
            name, _, value = words[k][2].partition("=")
            variables[name.lower()] = value
            k += 1
        asynchronous, first = False, True
        cands = []
        while k < len(words):
            value = words[k][2]
            b = _base(value)
            if b in ("start-process", "saps", "start") and first:
                rest = " ".join(w[2].lower() for w in words[k + 1:])
                asynchronous = not re.search(r"(^|\s)(-wait|/wait)\b", rest)
            if b in ("eval", "iex", "invoke-expression") and k + 1 < len(words) and depth < 3:
                out.extend(programs(" ".join(w[2] for w in words[k + 1:]), ps, depth + 1))
                break
            if b in WRAPPERS and b not in INTERPRETERS:
                k += 1
                first = False
                while k < len(words) and (words[k][2].startswith("-") or re.match(r"^/\w$", words[k][2]) or
                                          re.match(r"^\d+(\.\d+)?[smhd]?$", words[k][2])):
                    if words[k][2].lower() in ("-filepath",):
                        k += 1
                        break
                    k += 1
                continue
            break
        if k >= len(words):
            continue
        idx = [k]
        interp = _base(words[k][2])
        if interp in INTERPRETERS:
            j = k + 1
            takes = OPTION_VALUES.get(interp, ())
            while j < len(words) and (words[j][2].startswith(("-", "+")) or re.match(r"^/\w$", words[j][2])):
                opt = words[j][2].lower()
                if interp in CODE_INLINE and opt in CODE_INLINE[interp]:
                    j = len(words)  # python -c / node -e ...: program code, not a command line, and no script
                    break
                if opt in INLINE and interp not in CODE_INLINE:
                    # bash -c "...", pwsh -Command "...", cmd /c ...: a command line
                    inner = " ".join(w[2] for w in words[j + 1:]) if interp == "cmd" else \
                        (words[j + 1][2] if j + 1 < len(words) else "")
                    if depth < 3:
                        out.extend(programs(inner, interp in ("pwsh", "powershell"), depth + 1))
                    j = len(words)
                    break
                if interp in ("pwsh", "powershell") and opt in ENCODED and j + 1 < len(words):
                    if depth < 3:
                        out.extend(programs(decode_ps(words[j + 1][2]), True, depth + 1))
                    j = len(words)
                    break
                if opt in SCRIPT_NEXT.get(interp, ()):  # pwsh -File x.ps1: the next word is the script
                    j += 1
                    break
                j += 2 if opt in takes else 1  # -ExecutionPolicy Bypass, bash -o pipefail, python -X utf8 ...
            if j < len(words):
                idx.append(j)
        for j in idx:
            ws, we, value = words[j]
            prog = seg[ws:we]
            m = VARREF.match(value)
            if m and m.group(1).lower() in variables:
                prog = variables[m.group(1).lower()] + value[m.end():]
            rest = seg[we:]
            cands.append((prog + rest, len(prog), sep, asynchronous))
        out.extend(cands)
    return out


def match(command, tool, cwd, config):
    """What a command needs: None, or {resources: [...], exclusive, max_hold, rules: [match texts], background}.
    background: a matched command started with & / Start-Process (it would outlive its slot)."""
    ps = tool == "PowerShell"
    found, exclusive, holds, why, background = [], False, [], [], False
    rules = [r for r in config.rules if r.applies(tool, cwd)]
    if not rules:
        return None
    for text, plen, sep, asynchronous in programs(command or "", ps):
        for rule in rules:
            if not any(m.start() < plen for m in rule.rx.finditer(text)):
                continue
            if rule.resource.lower() != "none":
                for r in rule.resource_for(text).split("+"):
                    if r and r not in found:
                        found.append(r)
                exclusive = exclusive or rule.exclusive
                if rule.max_hold is not None:
                    holds.append(float(rule.max_hold))
                why.append(rule.match_text)
                background = background or asynchronous or (sep == "&" and not ps)
            break
    if not found:
        return None
    return {"resources": found, "exclusive": exclusive, "max_hold": max(holds) if holds else None, "rules": why,
            "background": background}


# ------------------------------------------------------------------------------------------- hand-rolled waits

LOOP = re.compile(r"\b(while|until)\b|\bfor\s*\(\s*;\s*;|\bdo\s*\{", re.IGNORECASE)
SLEEPS = re.compile(r"\bsleep\b|\bStart-Sleep\b|\btimeout(\.exe)?\s+/t\b|\bping\s+-n\s+\d+", re.IGNORECASE)
PROCESS_POLL = re.compile(r"\b(tasklist|pgrep|pidof|Get-Process|gps|ps|wmic\s+process|Get-CimInstance\s+Win32_Process|"
                          r"Get-WmiObject\s+Win32_Process)\b|\bkill\s+-0\b|\bkill\s+-s\s*0\b", re.IGNORECASE)
LOCK_POLL = re.compile(r"(\[\[?\s*!?\s*-[edfs]\s|\btest\s+!?\s*-[edfs]\s|\bTest-Path\b|\bos\.path\.exists\b)"
                       r"[^\n;]*lock", re.IGNORECASE)


def hand_rolled_wait(command):
    """'a process' / 'a lock file' when the command is a loop that waits for another job (polls the process list or a
    lock file and sleeps), else None."""
    cmd = command or ""
    if not (LOOP.search(cmd) and SLEEPS.search(cmd)):
        return None
    if PROCESS_POLL.search(cmd):
        return "another process"
    if LOCK_POLL.search(cmd):
        return "a lock file"
    return None


# ----------------------------------------------------------------------------------------------- hook rewrite

WRAPPED = re.compile(r"\bslot\s+run\b")


def label_of(inp, command):
    text = str(inp.get("description") or "").strip() or " ".join(command.split())[:60]
    text = re.sub(r"[\"'`$\\\r\n]+", " ", text)
    return " ".join(text.split())[:80] or "job"


def _sq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _psq(s):
    return "'" + s.replace("'", "''") + "'"


def rewrite(tool, inp, need, launcher, python=None):
    """The tool input with its command run under `slot run` (the command travels in env DEVTIME_SLOT_CMD so no shell
    quoting or path conversion can change it), or None if it can't be wrapped."""
    python = (python or sys.executable).replace("\\", "/")
    launcher = launcher.replace("\\", "/")
    cmd = inp.get("command") or ""
    label = label_of(inp, cmd)
    args = "slot run %s%s%s" % ("+".join(need["resources"]), " --exclusive" if need.get("exclusive") else "",
                                " --max-hold %g" % need["max_hold"] if need.get("max_hold") is not None else "")
    if tool == "Bash":
        conv = "MSYS2_ENV_CONV_EXCL=DEVTIME_SLOT_CMD " if os.name == "nt" else ""
        new = "%sDEVTIME_SLOT_CMD=%s %s %s %s --label %s --shell bash" % (conv, _sq(cmd), _sq(python), _sq(launcher),
                                                                          args, _sq(label))
    elif tool == "PowerShell":
        if re.search(r"^'@", cmd, re.MULTILINE):
            return None
        new = "$env:DEVTIME_SLOT_CMD = @'\n%s\n'@\n& %s %s %s --label %s --shell powershell" % (
            cmd, _psq(python), _psq(launcher), args, _psq(label))
    else:
        return None
    return dict(inp, command=new)


_CONFIG = None
_STAMP = None


def _stamp():
    out = []
    for path in (SHIPPED, user_file(), learned_file()):
        try:
            st = os.stat(path)
            out.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((path, None, None))
    return tuple(out)


def config():
    """The merged configuration, re-read when one of its files changed (three stat calls: cheap enough per call, so
    a newly learned or added rule counts from the next command on, in long-lived processes too)."""
    global _CONFIG, _STAMP
    stamp = _stamp()
    if _CONFIG is None or stamp != _STAMP:
        _CONFIG, _STAMP = Config(), stamp
    return _CONFIG


def first_rule(command, tool, cwd, cfg, ps=None):
    """The first rule (resource "none" included) that matches a program of the command, or None."""
    ps = tool == "PowerShell" if ps is None else ps
    rules = [r for r in cfg.rules if r.applies(tool, cwd)]
    for text, plen, _sep, _async in programs(command or "", ps):
        for rule in rules:
            if any(m.start() < plen for m in rule.rx.finditer(text)):
                return rule
    return None


def hook_check(tool, inp, cwd, launcher=None, cfg=None, subagent=True):
    """For the PreToolUse hook: None (leave the call alone), ("deny", reason) or ("wrap", new input, note).
    A main session's run_in_background wait for another job is let through (it costs nothing until it ends and then
    wakes the session); subagents and foreground waits are denied."""
    if tool not in ("Bash", "PowerShell"):
        return None
    cmd = str(inp.get("command") or "")
    if not cmd or not enabled(cmd) or WRAPPED.search(cmd):
        return None
    launcher = launcher or paths.launcher_path()
    show = 'python "%s" slot' % launcher.replace("\\", "/")
    what = hand_rolled_wait(cmd)
    if what and (subagent or not inp.get("run_in_background")):
        return ("deny", "devtime slots: this loop waits for %s to finish. Don't hand-roll waits for other jobs (they "
                        "hold a session or agent for as long as the other job runs and nobody sees them in the queue): "
                        "run your own job under a machine-wide slot instead, `%s run <resource> --label \"...\" -- "
                        "<command>` (it queues FIFO behind the other jobs and starts the moment its slot is free; "
                        "`%s status` shows holders and queue). Commands that match the slot rules are queued "
                        "automatically. If you are waiting for your own background task, read its output instead."
                % (what, show, show))
    cfg = cfg or config()
    need = match(cmd, tool, cwd, cfg)
    if not need:
        return None
    res = "+".join(need["resources"])
    if need["background"]:
        return ("deny", "devtime slots: this starts a '%s' job in the background with & / Start-Process, so it would "
                        "outlive its machine-wide slot (or be killed with it). Run it in the foreground of the command "
                        "and use the tool's run_in_background option instead; it is then queued for the slot "
                        "automatically." % res)
    new = rewrite(tool, inp, need, launcher)
    if new is None:
        return ("deny", "devtime slots: this command needs the machine-wide '%s' slot but can't be wrapped "
                        "automatically; run it as `%s run %s --label \"...\" -- <command>`." % (res, show, res))
    note = ("devtime slots: this command is queued for the machine-wide '%s' slot (FIFO across all sessions on this "
            "PC; `%s status` shows the queue). Queue time counts toward the tool timeout, so use run_in_background for "
            "long jobs. Put DEVTIME_SLOTS=0 in a command to run it unqueued." % (res, show))
    return ("wrap", new, note)
