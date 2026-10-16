"""Where DevTime keeps its data (nothing runtime-generated lives in the plugin / checkout), one instance per organisation.

One account can be signed into several organisations on one PC; they all share ~/.claude. Each organisation gets its
own devtime: its own data, its own server and port, its own dashboard. Nothing of one org is ever read into another's.

    DATA         DEVTIME_HOME, else ~/.claude/devtime                       shared by every org
    USER_CONFIG  DATA/config       devtime.toml (setting overrides), projects/*.toml, changes.toml, experiments.toml,
                 installed.json (what optimize changed, so remove can undo it)                       shared
    LOGS         DATA/logs         archived Unreal logs                                                shared
    DATA/plugin_root.txt, DATA/devtime.py   the plugin's current install path and a launcher that runs
                 `python -m devtime <args>` from it (text in CLAUDE.md can't use ${CLAUDE_PLUGIN_ROOT}); written by
                 hooks/ensure_server.py at every session start; the launched command picks the caller's org itself
    INSTANCE     DATA/orgs/<org uuid>                                         the current org's instance
    CACHE        INSTANCE/cache    transcript caches, alerts, plan samples, labels, review log and notes, server log,
                 port.txt ...

The current org (ORG) is, in this order: env CLAUDE_CODE_ORGANIZATION_UUID (desktop sessions, their hooks and
subprocesses), else <claude config>/.claude.json oauthAccount.organizationUuid (the CLI login; ~/.claude.json, or
$CLAUDE_CONFIG_DIR/.claude.json), else None. With no org every entry point refuses (fail closed); CACHE then points at
DATA/orgs/none, which holds no data because a scan without an org accepts no transcript.

Stdlib only and cheap to import: the hook scripts import it. Directories are created lazily with ensure().
"""

import hashlib
import json
import os
import re

ORG_ENV = "CLAUDE_CODE_ORGANIZATION_UUID"
_UUID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{3,63}$")


def claude_json_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    if base:
        return os.path.join(os.path.expanduser(base), ".claude.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def account():
    """The oauthAccount record of the .claude.json ({} if missing or unreadable)."""
    try:
        with open(claude_json_path(), encoding="utf-8") as f:
            acct = json.load(f).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return {}
    return acct if isinstance(acct, dict) else {}


def current_org():
    """(org uuid, source) of the org this process runs in: (uuid, "env"), (uuid, "claude.json") or (None, None).
    Only plain ids are accepted (they name a folder)."""
    value = (os.environ.get(ORG_ENV) or "").strip()
    if value:
        return (value, "env") if _UUID.match(value) else (None, None)
    org = account().get("organizationUuid")
    if isinstance(org, str) and _UUID.match(org.strip()):
        return org.strip(), "claude.json"
    return None, None


def org_name(uuid):
    """The org's name from the .claude.json when it is that login's org, else the first 8 characters of its id."""
    acct = account()
    name = acct.get("organizationName")
    if uuid and acct.get("organizationUuid") == uuid and isinstance(name, str) and name.strip():
        return name.strip()
    return (uuid or "unknown")[:8]


DATA = os.path.abspath(os.path.expanduser(os.environ.get("DEVTIME_HOME") or os.path.join("~", ".claude", "devtime")))
LOGS = os.path.join(DATA, "logs")
USER_CONFIG = os.path.join(DATA, "config")
LEGACY_CACHE = os.path.join(DATA, "cache")  # before 0.4.0: one cache for every org (migrate.py splits it)
ORG, ORG_SOURCE = current_org()
INSTANCE = os.path.join(DATA, "orgs", ORG or "none")
CACHE = os.path.join(INSTANCE, "cache")

NO_ORG_TEXT = ("devtime does nothing here: the organisation of this session could not be determined (no %s and no "
               "oauthAccount in %s)" % (ORG_ENV, claude_json_path()))


def instance_of(org):
    return os.path.join(DATA, "orgs", org)


def home_id():
    """Identifies this org's instance folder (first 12 hex of the SHA-1 of its absolute path): a server is ours only
    if it reports it, so each org's sessions find their own org's server."""
    return hashlib.sha1(os.path.normcase(os.path.abspath(INSTANCE)).encode("utf-8")).hexdigest()[:12]


def allowed_orgs(settings=None):
    """The transcript orgs this instance uses: its own org, plus "unknown" (old transcripts without an org tag that
    the desktop app's index does not know either) when settings.include_unknown_org is true. Empty without an org."""
    if not ORG:
        return frozenset()
    return frozenset([ORG] + (["unknown"] if (settings or {}).get("include_unknown_org") else []))


def ensure(path):
    """Creates the directory (or the parent directory of a file path with an extension) and returns the path."""
    os.makedirs(path, exist_ok=True)
    return path


def cache_file(name):
    """Path of a file in CACHE, creating CACHE."""
    return os.path.join(ensure(CACHE), name)


LAUNCHER = '''"""Stable entry point for DevTime (written by the plugin at session start; the plugin path changes on update).

    python devtime.py <args>      same as `python -m devtime <args>` run from the root in plugin_root.txt
"""
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(here, "plugin_root.txt"), encoding="utf-8") as f:
        root = f.read().strip()
except OSError:
    sys.exit("devtime: plugin_root.txt is missing; start a Claude Code session with the devtime plugin enabled")
sys.exit(subprocess.call([sys.executable, "-m", "devtime"] + sys.argv[1:], cwd=root))
'''


def launcher_path():
    return os.path.join(DATA, "devtime.py")


def install_launcher(root=None):
    """Records the plugin root and writes the launcher into DATA (only files whose content changed). Returns the
    launcher's path."""
    root = os.path.abspath(root or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ensure(DATA)
    for name, text in (("plugin_root.txt", root + "\n"), ("devtime.py", LAUNCHER)):
        path = os.path.join(DATA, name)
        try:
            with open(path, encoding="utf-8", newline="") as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    return launcher_path()
