"""The weekly reserve: a safety margin so the week never runs out by accident.

    usable = 100 - reserve_per_day x days until the weekly reset      (clamped to 0..100)

Sessions stop starting new agents or stages at or above it. The rule sessions follow is the one in the devtime block of
<claude dir>/CLAUDE.md (written by `setup claude-md`): a reserve per day, or a fixed cap (a number) when the user chose
that. Without a block the settings decide: `weekly_cap` (a number) if set, else `weekly_reserve_per_day` (default 5).
"""

DEFAULT_PER_DAY = 5.0
DAY = 86400.0


def usable(per_day, days_left):
    """Percent of the weekly limit usable with `days_left` days (fractions allowed) before the reset."""
    return max(0.0, min(100.0, 100.0 - float(per_day) * max(0.0, float(days_left))))


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def settings_rule(settings):
    """{"mode": "cap", "value": W} or {"mode": "reserve", "value": N} from settings."""
    settings = settings or {}
    cap = number(settings.get("weekly_cap"))
    if cap is not None:
        return {"mode": "cap", "value": cap}
    per_day = number(settings.get("weekly_reserve_per_day"))
    return {"mode": "reserve", "value": DEFAULT_PER_DAY if per_day is None else per_day}


def rule(settings=None):
    """The rule sessions follow: the CLAUDE.md block's if there is one, else the settings'."""
    try:
        from . import setup
        found = setup.claude_md_weekly()
    except Exception:
        found = None
    return found or settings_rule(settings)


def usable_now(rule_, reset, now):
    """Percent usable now under `rule_`, None when a reserve is wanted but the reset time is unknown."""
    if rule_["mode"] == "cap":
        return max(0.0, min(100.0, rule_["value"]))
    if not reset:
        return None
    return usable(rule_["value"], (reset - now) / DAY)


def describe(rule_):
    return ("%g %% per day until the reset" % rule_["value"]) if rule_["mode"] == "reserve" else "fixed cap %g %%" % rule_["value"]
