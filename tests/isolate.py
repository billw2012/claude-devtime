"""Imported first by every test module: points devtime at a throw-away home before it is imported, so no test can read
or write the real ~/.claude, ~/.claude.json or devtime data dir (paths.py computes its folders and the current org at
import). Tests that need specific folders still patch devtime.paths."""

import atexit
import os
import shutil
import tempfile

ORG = "cccc3333-0000-4000-8000-000000000003"  # the org the test process "runs in" unless a test says otherwise
HOME = tempfile.mkdtemp(prefix="devtime-test-home-")
atexit.register(shutil.rmtree, HOME, True)
for key in ("HOME", "USERPROFILE"):
    os.environ[key] = HOME
os.environ["DEVTIME_HOME"] = os.path.join(HOME, ".claude", "devtime")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(HOME, ".claude")
os.environ["DEVTIME_DESKTOP_DIR"] = os.path.join(HOME, "Claude")
os.environ["CLAUDE_CODE_ORGANIZATION_UUID"] = ORG
os.environ["DEVTIME_NO_SERVER_START"] = "1"
os.environ["DEVTIME_SLOTS_DIR"] = os.path.join(HOME, "slots")  # never the machine's real slot queue

# Parallel support (slot queue, agent budget rules) is off by default; most tests exercise it, so the test home turns it
# on. Tests of the default or of a switch use set_parallel() below.
CONFIG_FILE = os.path.join(os.environ["DEVTIME_HOME"], "config", "devtime.toml")
os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
with open(CONFIG_FILE, "w", encoding="utf-8") as _f:
    _f.write("[settings]\nparallel = true\n")


def set_parallel(text="parallel = true\n"):
    """Replaces the test home's [settings] (text = its lines; None removes the file) and drops the settings cache."""
    from devtime import config
    if text is None:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("[settings]\n" + text)
    config._USER_SETTINGS.clear()
