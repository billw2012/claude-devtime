"""Status line command (user-wide): records the plan usage Claude Code passes to it, then prints a short status line.

Claude Code gives the status line command `rate_limits.five_hour` / `rate_limits.seven_day` (`used_percentage`,
`resets_at`) after each API response on a Pro/Max plan. This appends them to <data dir>/cache/plan_samples.jsonl (the same
readings `python -m devtime plan-sample` writes) when they change or every 10 minutes, so the weekly-limit chart gets
exact readings from every session without a scheduled task. Never fails: prints nothing usable is fine.
"""

import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from devtime import paths  # noqa: E402

SAMPLES = os.path.join(paths.CACHE, "plan_samples.jsonl")
LAST = os.path.join(paths.CACHE, "statusline_last.json")
EVERY_S = 600


def epoch(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def record(rl, now):
    five, week = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    if five.get("used_percentage") is None and week.get("used_percentage") is None:
        return None
    row = {"t": round(now, 1), "fh": five.get("used_percentage"), "sd": week.get("used_percentage"),
           "fhReset": epoch(five.get("resets_at")), "sdReset": epoch(week.get("resets_at")), "src": "statusline"}
    try:
        with open(LAST, encoding="utf-8") as f:
            last = json.load(f)
    except (OSError, ValueError):
        last = {}
    same = last.get("fh") == row["fh"] and last.get("sd") == row["sd"]
    if not same or now - last.get("t", 0) >= EVERY_S:
        os.makedirs(os.path.dirname(SAMPLES), exist_ok=True)
        with open(SAMPLES, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        tmp = LAST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f)
        os.replace(tmp, LAST)
    return row


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    row = None
    try:  # when it last ran and which fields it got (names only), to tell "not run" from "no rate_limits"
        os.makedirs(paths.CACHE, exist_ok=True)
        with open(os.path.join(paths.CACHE, "statusline_seen.json"), "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "keys": sorted(data), "rate_limits": sorted(data.get("rate_limits") or {})}, f)
    except Exception:
        pass
    try:
        row = record(data.get("rate_limits") or {}, time.time())
    except Exception:
        pass
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    if row:
        if row["fh"] is not None:
            parts.append(f"5h {row['fh']:.0f}%")
        if row["sd"] is not None:
            parts.append(f"week {row['sd']:.0f}%")
    print(" · ".join(parts))


if __name__ == "__main__":
    main()
