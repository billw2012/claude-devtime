"""`python -m devtime setup ...`: the mechanical half of the /devtime:start skill. Output is compact JSON or short text.

    setup check                       python, server, recent projects, CLAUDE.md block, duplicate hooks, env, data dir
    setup claude-md --cap N [--dry-run]   insert / replace the devtime block in <claude dir>/CLAUDE.md
    setup open                        start the server if needed, open the dashboard in the browser

<claude dir> is CLAUDE_CONFIG_DIR if set, else ~/.claude.
"""

import difflib
import importlib.util
import json
import os
import re
import sys
import time
import webbrowser

from . import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = "<!-- devtime:start -->"
END = "<!-- devtime:end -->"
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
CAP_RE = re.compile(r"\bat\s+(\d+(?:\.\d+)?)\s*%\s+of the weekly")
FIVE_RE = re.compile(r"\bat\s+(\d+(?:\.\d+)?)\s*%\s+of the 5-hour")
HOOK_SCRIPTS = ("agent_budget.py", "ensure_server.py")

BLOCK = """{start}
## Usage tracking (devtime)
- On long or autonomous runs, call `mcp__ccd_session_mgmt__get_usage` before launching each agent stage (it records the plan's 5-hour and weekly usage for the devtime dashboard; there is no other source).
- Weekly cap: at {cap} % of the weekly all-models limit start no new agents or stages; let running ones reach a safe point, commit, stop and tell the user what was left undone. Only the user raises the cap.
{five}- Name subagents with a short description of the piece of work (it labels work sections on the dashboard).
{end}"""


def claude_dir():
    return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join("~", ".claude")))


def claude_md_path():
    return os.path.join(claude_dir(), "CLAUDE.md")


def settings_path():
    return os.path.join(claude_dir(), "settings.json")


def fmt_cap(cap):
    return "%g" % float(cap)


FIVE_LINE = ("- 5-hour cap: at {five} % of the 5-hour limit start no new agents or stages; let running ones reach a safe "
             "point, then wait until the window's `resetsAt` (schedule a wakeup just after it), re-check usage and "
             "continue.\n")


def make_block(cap, eol="\n", five=None):
    line = FIVE_LINE.format(five=fmt_cap(five)) if five is not None else ""
    return BLOCK.format(start=START, end=END, cap=fmt_cap(cap), five=line).replace("\n", eol)


def block_caps(block):
    """(weekly cap, 5-hour cap) found in a devtime block's text, None where absent."""
    found = [r.search(block or "") for r in (CAP_RE, FIVE_RE)]
    return tuple(float(m.group(1)) if m else None for m in found)


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except FileNotFoundError:
        return None


def upsert_block(text, block_re, make):
    """Returns (new text, action): the block make(eol) inserted at the end of `text` (None = no file yet) or replacing
    the one block_re finds; everything else is kept as is. `make` gets the file's line ending."""
    if text is None:
        return make("\n") + "\n", "created"
    eol = "\r\n" if "\r\n" in text else "\n"
    block = make(eol)
    m = block_re.search(text)
    if m:
        out = text[:m.start()] + block + text[m.end():]
        return out, "unchanged" if out == text else "replaced"
    if text and not text.endswith("\n"):
        text += eol
    if text.strip():
        text += eol
    return text + block + eol, "inserted"


def remove_block(text, block_re):
    """Returns (new text, removed): the block, its line end and the blank line that separated it dropped."""
    m = block_re.search(text or "")
    if not m:
        return text, False
    eol = "\r\n" if "\r\n" in text else "\n"
    head, tail = text[:m.start()], text[m.end():]
    if tail.startswith(eol):
        tail = tail[len(eol):]
        if not head and tail.startswith(eol):  # first thing in the file: the blank line after it goes too
            tail = tail[len(eol):]
    if head.endswith(eol * 2):
        head = head[:-len(eol)]
    return head + tail, True


def new_claude_md(text, cap, five=None):
    """Returns (new text, action) with the devtime block inserted or replaced; everything else is kept as is."""
    return upsert_block(text, BLOCK_RE, lambda eol: make_block(cap, eol, five))


def claude_md_cmd(cap, dry_run=False, five=None):
    path = claude_md_path()
    old = read_text(path)
    new, action = new_claude_md(old, cap, five)
    if dry_run:
        diff = "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                            "a/CLAUDE.md" if old is not None else "/dev/null", "b/CLAUDE.md"))
        print(json.dumps({"path": path, "action": action, "dryRun": True, "cap": fmt_cap(cap)}, separators=(",", ":")))
        print(diff or "(no change)")
        return
    if new != old:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(new.encode("utf-8"))
    print(json.dumps({"path": path, "action": action, "cap": fmt_cap(cap)}, separators=(",", ":")))


def norm(p):
    return os.path.normpath(p).replace("\\", "/").lower()


def find_hook_duplicates(settings, root=ROOT):
    """Hook commands in a settings dict that reference this plugin's scripts from outside the plugin root."""
    found = []
    root_n = norm(root)
    for event, groups in ((settings or {}).get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                cmd = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(cmd, str) or not any(s in cmd for s in HOOK_SCRIPTS):
                    continue
                flat = cmd.replace("\\", "/").lower()
                if "${claude_plugin_root}" in flat or root_n in flat:
                    continue
                found.append({"event": event, "matcher": group.get("matcher"), "command": cmd})
    return found


def load_hooks_module():
    spec = importlib.util.spec_from_file_location("devtime_ensure_server",
                                                  os.path.join(ROOT, "hooks", "ensure_server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recent_projects(days=7):
    from .store import Store
    store = Store(verbose=False)
    store.refresh()
    since = time.time() - days * 86400
    agg = {}
    for t in store.data["transcripts"]:
        if t["end"] >= since:
            p = agg.setdefault(t["project"], {"name": t["project"], "cost": 0.0, "busyH": 0.0})
            p["cost"] += t["cost"]
            p["busyH"] += t["busy"] / 3600
    return sorted(({"name": p["name"], "cost": round(p["cost"], 2), "busyH": round(p["busyH"], 1)}
                   for p in agg.values()), key=lambda p: -p["cost"])


def check(projects=True):
    hooks = load_hooks_module()
    number = hooks.port()
    text = read_text(claude_md_path())
    m = BLOCK_RE.search(text) if text else None
    cap, five = block_caps(m.group(0) if m else None)
    settings, settings_error = {}, None
    raw = read_text(settings_path())
    if raw is not None:
        try:
            settings = json.loads(raw)
        except ValueError as error:
            settings_error = str(error)
    env = (settings or {}).get("env") or {}
    out = {
        "python": "%d.%d.%d" % sys.version_info[:3], "pythonOk": sys.version_info >= (3, 11),
        "server": {"url": "http://%s:%d/" % (hooks.HOST, number), "answering": hooks.ours(number)},
        "claudeDir": claude_dir(),
        "claudeMd": {"path": claude_md_path(), "exists": text is not None, "block": m is not None,
                     "cap": cap, "fiveHourCap": five},
        "settings": {"path": settings_path(), "exists": raw is not None, "error": settings_error,
                     "duplicateHooks": find_hook_duplicates(settings),
                     "autoCompactWindow": env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")},
        "dataDir": paths.DATA, "pluginRoot": ROOT,
        "org": {"id": paths.ORG, "name": paths.org_name(paths.ORG), "source": paths.ORG_SOURCE,
                "instance": paths.INSTANCE},
    }
    if projects:
        try:
            out["projects7d"] = recent_projects()
        except Exception as error:
            out["projects7d"] = {"error": repr(error)}
    print(json.dumps(out, separators=(",", ":")))
    return out


def ensure_server(wait_s=60):
    """Starts the server if it is not answering (detached; no-op if another start is under way) and waits for it.
    Returns (url, answering). DEVTIME_NO_SERVER_START=1 only checks (tests)."""
    hooks = load_hooks_module()
    number = hooks.port()  # this data folder's port (devtime/ports.py), not just whatever answers on 8765
    if not hooks.ours(number) and not os.environ.get("DEVTIME_NO_SERVER_START"):
        hooks.main()
        number = hooks.port()
        deadline = time.time() + wait_s
        while time.time() < deadline and not hooks.ours(number):
            time.sleep(1)
    return "http://%s:%d/" % (hooks.HOST, number), hooks.ours(number)


def claude_md_status():
    """(block exists, weekly cap or None, 5-hour cap or None) for <claude dir>/CLAUDE.md."""
    text = read_text(claude_md_path())
    m = BLOCK_RE.search(text) if text else None
    return (m is not None,) + block_caps(m.group(0) if m else None)


def open_cmd(wait_s=60):
    url, ok = ensure_server(wait_s)
    if not ok:
        print("server not answering on %s after %d s; see %s" % (url, wait_s, load_hooks_module().LOG))
        return 1
    webbrowser.open(url)
    print(url)
    return 0


def main(args):
    """args: argparse namespace with setup_cmd, cap, dry_run."""
    if args.setup_cmd == "check":
        check()
    elif args.setup_cmd == "claude-md":
        claude_md_cmd(args.cap, args.dry_run, getattr(args, "five_hour_cap", None))
    elif args.setup_cmd == "open":
        return open_cmd()
