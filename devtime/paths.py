"""Where DevTime keeps its per-user data (nothing runtime-generated lives in the plugin / checkout).

    DATA         DEVTIME_HOME, else ~/.claude/devtime
    CACHE        DATA/cache        transcript caches, alerts, plan samples, server log ...
    LOGS         DATA/logs         archived Unreal logs
    USER_CONFIG  DATA/config       devtime.toml (setting overrides), projects/*.toml, changes.toml, experiments.toml,
                 installed.json (what optimize changed, so remove can undo it)
    DATA/plugin_root.txt, DATA/devtime.py   the plugin's current install path and a launcher that runs
                 `python -m devtime <args>` from it (text in CLAUDE.md can't use ${CLAUDE_PLUGIN_ROOT}); written by
                 hooks/ensure_server.py at every session start

Stdlib only and cheap to import: the hook scripts import it. Directories are created lazily with ensure().
"""

import hashlib
import os

DATA = os.path.abspath(os.path.expanduser(os.environ.get("DEVTIME_HOME") or os.path.join("~", ".claude", "devtime")))
CACHE = os.path.join(DATA, "cache")
LOGS = os.path.join(DATA, "logs")
USER_CONFIG = os.path.join(DATA, "config")


def home_id():
    """Identifies this data folder (first 12 hex of the SHA-1 of its absolute path): a server is ours if it reports it."""
    return hashlib.sha1(os.path.normcase(os.path.abspath(DATA)).encode("utf-8")).hexdigest()[:12]


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
