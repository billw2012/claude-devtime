"""What counts as devtime's own Claude usage.

Two sources, both matched by NAME only (a slash-command / skill / agent-type name; no prompt text is read or kept):

- subagents whose agentType is devtime:<something> other than the agents that do the user's own work;
- main-session turns driven by a devtime skill: from a /devtime:<skill> slash command (or a Skill tool call of a
  devtime:* skill) until the next user prompt that is not a devtime command.

Categories: "loop" (/devtime:start, including the loop's "tick" pass), "labeller", "reviewer", "setup & optimize"
(every other devtime skill or agent).
"""

import re

CATEGORIES = ("loop", "labeller", "reviewer", "setup & optimize")
USER_AGENTS = ("devtime:coder", "devtime:reader", "devtime:runner")  # these do the user's own work
COMMAND_RE = re.compile(r"<command-name>\s*/?(devtime[:-][\w-]+)\s*</command-name>|^\s*/(devtime[:-][\w-]+)")


def command_name(text):
    """The devtime skill a user message invokes as a slash command ("devtime:start"), else None. Only the name."""
    m = COMMAND_RE.search(text[:400]) if isinstance(text, str) else None
    if not m:
        return None
    name = (m.group(1) or m.group(2)).lower()
    return "devtime:" + name[8:]


def skill_category(name):
    """Category of a devtime skill name ("devtime:start", "devtime-setup"), None for any other skill."""
    name = (name or "").strip().lstrip("/").lower()
    if not (name.startswith("devtime:") or name.startswith("devtime-")) or len(name) <= 8:
        return None
    return "loop" if name == "devtime:start" else "setup & optimize"


def agent_category(agent_type):
    """Category of a subagent type, None when it is not devtime's own usage."""
    name = (agent_type or "").strip().lower()
    if not name.startswith("devtime:") or len(name) <= 8 or name in USER_AGENTS:
        return None
    return {"devtime:labeller": "labeller", "devtime:reviewer": "reviewer"}.get(name, "setup & optimize")
