"""SessionStart hook: make sure the dashboard server is running.

If nothing answers on the dashboard's port, start `python -m devtime serve --no-open` from the plugin root (this file's
parent directory) as a detached, windowless background process (it outlives the session), logging to
<data dir>/cache/server.log. The port is settings.port (shipped config, then the user's devtime.toml), default 8765.
Also records the plugin root and a launcher in the data dir (paths.install_launcher) so CLAUDE.md text can run
`python <data dir>/devtime.py ...` without knowing the plugin's install path. Always exits 0 and prints nothing, so it never gets in a session's way. Usage alerts, the plan readings and the usage
review all need the server.
"""

import os
import socket
import subprocess
import sys
import time
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from devtime import paths  # noqa: E402

HOST = "127.0.0.1"
LOG = os.path.join(paths.CACHE, "server.log")
LOCK = os.path.join(paths.CACHE, "server.starting")


def port():
    value = 8765
    for path in (os.path.join(ROOT, "config", "devtime.toml"), os.path.join(paths.USER_CONFIG, "devtime.toml")):
        try:
            with open(path, "rb") as f:
                value = int(tomllib.load(f).get("settings", {}).get("port", value))
        except (OSError, ValueError):
            pass
    return value


def answering(number):
    try:
        with socket.create_connection((HOST, number), timeout=0.5):
            return True
    except OSError:
        return False


def main():
    try:  # the stable launcher (see paths.py); never in the way of starting the server
        paths.install_launcher(ROOT)
    except Exception:
        pass
    if answering(port()):
        return
    os.makedirs(paths.CACHE, exist_ok=True)
    try:  # several sessions may start at once: only one launches; a stale lock (a failed start) expires
        if time.time() - os.path.getmtime(LOCK) < 60:
            return
    except OSError:
        pass
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    if os.path.exists(LOG) and os.path.getsize(LOG) > 5_000_000:
        os.replace(LOG, LOG + ".old")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with open(LOG, "a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, "-u", "-m", "devtime", "serve", "--no-open"], cwd=ROOT, stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
                         close_fds=True, start_new_session=os.name != "nt")


if __name__ == "__main__":
    try:
        sys.stdin.read()  # the hook's JSON input; not needed
    except Exception:
        pass
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
