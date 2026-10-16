"""Plan-usage readings reported by the sessions themselves (the plugin's session.measure hook -> POST /api/reading).

Every session knows its account's 5-hour and weekly usage (the rate-limit headers of its last response). The hook posts
{session, at, fiveHour, fiveHourReset, weekly, weeklyReset} (percent used and ISO reset times); this module resolves
the session's organisation and appends a sample to the plan samples file in the format `devtime plan-sample` writes
(t, fh, sd, fhReset, sdReset, org). Numbers, reset times and the session id only.

    - identical consecutive readings of an org are not stored twice;
    - a stale reading (an idle session repeating what it saw earlier: the same weekly window with a lower share, or an
      earlier weekly window) is dropped, so it cannot look like a weekly reset;
    - a session whose org is not known yet (no transcript scanned) is queued and recorded once it is, or as "unknown"
      after QUEUE_S.
"""

import json
import os
import threading
import time

from . import orgs

QUEUE_S = 900
QUEUE_MAX = 100
MAX_SKEW = 600  # seconds a reading's own time may differ from now before the server's clock is used


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 1000 else None


def parse(body, now=None):
    """The validated reading {session, t, fh, sd, fhReset, sdReset} of a request body, or None."""
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        return None
    session = body.get("session")
    fh, sd = _percent(body.get("fiveHour")), _percent(body.get("weekly"))
    if not isinstance(session, str) or not session or len(session) > 200 or (fh is None and sd is None):
        return None
    if sd is None:  # the weekly share is what the fit needs; a 5-hour-only reading carries nothing for the limit view
        return None
    t = body.get("at")
    t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and abs(t - now) <= MAX_SKEW else now
    reset = lambda key: _iso(body.get(key))
    return {"session": session, "t": round(t, 1), "fh": fh if fh is not None else 0.0, "sd": sd,
            "fhReset": reset("fiveHourReset"), "sdReset": reset("weeklyReset")}


def _iso(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        from .overview import iso_ts
        return iso_ts(text)
    except ValueError:
        return None


class Readings:
    """Appends readings to the plan samples file; thread-safe (the server handles requests on threads)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.last = {}  # org -> last stored row
        self.pending = {}  # session -> (queued at, reading)

    def _last_of(self, org):
        if org not in self.last:
            row = None
            try:
                with open(self.path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("org") == org:
                            row = r
            except OSError:
                pass
            self.last[org] = row
        return self.last[org]

    def _store(self, reading, org):
        """Appends one row unless it repeats or is stale. Returns "recorded", "duplicate" or "stale"."""
        last = self._last_of(org)
        if last:
            same = all(last.get(k) == reading[k] for k in ("fh", "sd", "fhReset", "sdReset"))
            if same:
                return "duplicate"
            if reading["sdReset"] and last.get("sdReset") and (
                    reading["sdReset"] < last["sdReset"] - 60
                    or (abs(reading["sdReset"] - last["sdReset"]) <= 60 and reading["sd"] < last["sd"])):
                return "stale"
        row = {"t": reading["t"], "fh": reading["fh"], "sd": reading["sd"], "fhReset": reading["fhReset"],
               "sdReset": reading["sdReset"], "org": org}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        self.last[org] = row
        return "recorded"

    def add(self, reading, org):
        """Stores a reading for `org` (a full org id), or queues it when org is None. Returns the outcome word."""
        with self.lock:
            if org is None:
                if len(self.pending) >= QUEUE_MAX and reading["session"] not in self.pending:
                    self.pending.pop(next(iter(self.pending)))
                self.pending[reading["session"]] = (time.time(), reading)
                return "queued"
            self.pending.pop(reading["session"], None)
            return self._store(reading, org)

    def flush(self, org_of, now=None):
        """Records the queued readings whose session's org is known now (org_of(session) -> org id or None), and
        those queued longer than QUEUE_S under "unknown". Returns how many were recorded."""
        now = time.time() if now is None else now
        done = 0
        with self.lock:
            for session, (queued, reading) in list(self.pending.items()):
                org = org_of(session)
                if org is None and now - queued >= QUEUE_S:
                    org = orgs.UNKNOWN
                if org is not None:
                    del self.pending[session]
                    done += self._store(reading, org) == "recorded"
        return done
