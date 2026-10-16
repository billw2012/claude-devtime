"""Which port this org instance's dashboard server uses (stdlib only; imported by the hooks).

Each organisation on the PC has its own instance (paths.py) and server, and two OS users (or two data folders) must
never see each other's dashboard either, so a server only counts as ours if its /api/version `home` matches
paths.home_id() (a hash of the instance folder). settings.port (default 8765) is kept when it is ours or free; if
someone else's server holds it, the next free port of port+1..port+20 is used. The choice is recorded in the
instance's cache/port.txt, which every URL builder reads through choose() (setup, tick, hooks, server) and the hooks
module reads to post readings.
"""

import json
import os
import socket
import tomllib
import urllib.request

from . import paths

HOST = "127.0.0.1"
SPAN = 20
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def port_file():
    return os.path.join(paths.CACHE, "port.txt")


def configured(root=None):
    """settings.port: the shipped config, then the user's devtime.toml; default 8765."""
    value = 8765
    for path in (os.path.join(root or ROOT, "config", "devtime.toml"), os.path.join(paths.USER_CONFIG, "devtime.toml")):
        try:
            with open(path, "rb") as f:
                value = int(tomllib.load(f).get("settings", {}).get("port", value))
        except (OSError, ValueError):
            pass
    return value


def recorded():
    try:
        with open(port_file(), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def record(number):
    """Writes port.txt (only when it changes)."""
    if recorded() == number:
        return
    try:
        os.makedirs(paths.CACHE, exist_ok=True)
        tmp = "%s.%d.tmp" % (port_file(), os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("%d\n" % number)
        os.replace(tmp, port_file())
    except OSError:
        pass


def answering(number):
    try:
        with socket.create_connection((HOST, number), timeout=0.5):
            return True
    except OSError:
        return False


def home_of(number):
    """The `home` a server on this port reports, "" if it is a server without one, None if nothing devtime answers."""
    try:
        with urllib.request.urlopen("http://%s:%d/api/version" % (HOST, number), timeout=2) as response:
            return str(json.loads(response.read().decode("utf-8")).get("home") or "")
    except Exception:
        return None


def ours(number):
    return answering(number) and home_of(number) == paths.home_id()


def choose(root=None):
    """The port to use: one that is already ours, else the first free one from the configured port up (the configured
    port itself when everything is taken). Recorded in port.txt."""
    base = configured(root)
    candidates = list(range(base, base + SPAN + 1))
    last = recorded()
    if last is not None and last not in candidates:
        candidates.append(last)
    chosen = None
    for number in sorted(candidates, key=lambda n: (n != last, n)):
        if answering(number) and ours(number):
            chosen = number
            break
    if chosen is None:
        chosen = next((n for n in candidates if not answering(n)), base)
    record(chosen)
    return chosen
