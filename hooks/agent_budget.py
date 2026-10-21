"""Claude Code PreToolUse / PostToolUse hook (user-wide): keeps subagents cheap. Main sessions are never touched.

Measured on one heavy week of agent use: 84 % of cost was subagents, 27 % was subagents re-writing their whole prompt
cache after waiting more than 5 minutes on a build or editor run (a subagent's cache lives 5 minutes; a main session's
1 hour), and 80 % was requests with contexts over 300k (opus[1m] / 1M-context subagents never auto-compact). So, inside
a subagent only:

1. Slow waits are denied: sleeps / wait loops / Wait-Process / blocking TaskOutput or Monitor waits that may run over
   WAIT_LIMIT, or any wait once the agent has already spent WAIT_BUDGET waiting. A plain foreground job (a build, an
   editor run) with a long timeout is let through until one of the agent's calls has actually run over WAIT_LIMIT
   (PostToolUse's duration_ms); after that the agent is told, and further long jobs are denied. The reason tells the
   agent to hand the job back to the main session and end.
2. Past CONTEXT_NUDGE tokens of context (then every CONTEXT_STEP more) the agent is told to finish its step and end
   with a handoff so a fresh agent continues small.
3. Usage alerts (devtime/alerts.py, written by the session's org's dashboard server to
   <data dir>/orgs/<org>/cache/alerts.json): each new alert is passed once to the subagent it is about and to its main
   session, which can stop it, re-brief it or take its jobs over. This part also runs in main sessions. Only the
   instance of the session's own org is read (devtime/paths.py); if the org can't be determined, no alert is delivered.

Registered by the plugin's hooks/hooks.json (PreToolUse "*", PostToolUse "Bash|PowerShell|TaskOutput|Monitor"); run
through hooks/run.sh so the right Python is found.

5. Slots (devtime/slotrules.py, every session, Bash / PowerShell): a command matching a rule of the machine-wide
   slots.toml is rewritten (updatedInput) to run under `<launcher> slot run <resource> ...`, so it queues FIFO with
   every other session's heavy jobs; loops that wait for another process or a lock file are denied with a pointer to
   `slot run` (a main session's run_in_background wait is let through). In subagents such a loop also counts as a long wait whatever its timeout. Env DEVTIME_SLOTS=0 (or
   DEVTIME_SLOTS=0 in the command) leaves commands alone.

4. A/B experiments (devtime/ab.py): an Agent / Task launch whose description starts "AB:<experiment>:<stage>" of an
   active experiment in <data dir>/config/experiments.toml must use the model and agent type of the arm the stage's
   hash gives; otherwise it is denied with the right arm. Unknown or stopped experiments pass. Runs in main sessions too.

Features 1, 2 and 5 are parallel support, off by default: on with settings.parallel = true (or settings.features.slots /
settings.features.agent_budget) in the user's devtime.toml, `python -m devtime setup parallel on`. Without it the hook
only delivers usage alerts and enforces A/B experiments, which act only when the user set them up.

Never fails a call: any error inside the hook lets the call through. Exits silently on Python older than 3.11.
"""

import sys

if sys.version_info < (3, 11):  # devtime needs tomllib; a hook must never get in the way
    sys.exit(0)

import glob
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from devtime import paths  # noqa: E402  (stdlib only, no heavy imports: this runs on every tool call)

STATE_DIR = os.path.join(paths.CACHE, "agent_budget")
ALERTS = os.path.join(paths.CACHE, "alerts.json")
ALERT_FRESH_S = 1800  # an alert last seen longer ago than this is not delivered any more


def on(name):
    """Whether the feature ("slots", "agent_budget") is on (devtime/config.py: off unless settings.parallel or overridden)."""
    try:
        from devtime import config
        return config.feature(name)
    except Exception:
        return False


def dashboard():
    """The dashboard's URL: the port this data folder's server recorded (cache/port.txt), default 8765."""
    try:
        with open(os.path.join(paths.CACHE, "port.txt"), encoding="utf-8") as f:
            return "http://127.0.0.1:%d/" % int(f.read().strip())
    except (OSError, ValueError):
        return "http://127.0.0.1:8765/"


# agent types whose job is to wait (small context, Haiku by default): exempt from the wait rules
WAITERS = set(filter(None, os.environ.get("DEVTIME_AGENT_WAITERS", "runner").split(",")))
WAIT_LIMIT = float(os.environ.get("DEVTIME_AGENT_WAIT_LIMIT", 240))  # seconds; under the 5 min subagent cache life
WAIT_BUDGET = float(os.environ.get("DEVTIME_AGENT_WAIT_BUDGET", 600))  # total seconds of waits per subagent
CONTEXT_NUDGE = int(os.environ.get("DEVTIME_AGENT_CONTEXT_NUDGE", 250_000))
CONTEXT_STEP = 150_000
TAIL_BYTES = 400_000

SLEEP = re.compile(r"\b(?:sleep|Start-Sleep(?:\s+-(?:Seconds|s))?)\s+(\d+(?:\.\d+)?)\s*([smh]?)\b", re.IGNORECASE)
START_SLEEP_MS = re.compile(r"Start-Sleep\s+-(?:Milliseconds|m)\s+(\d+)", re.IGNORECASE)
TIMEOUT_T = re.compile(r"\btimeout(?:\.exe)?\s+/t\s+(\d+)", re.IGNORECASE)
DEADLINE = re.compile(r"Add(Seconds|Minutes)\((\d+)\)", re.IGNORECASE)
LOOP = re.compile(r"\b(until|while)\b", re.IGNORECASE)
BLOCKING = re.compile(r"\bWait-Process\b|\bWait-Job\b|\bwait\s+\$!|^\s*wait\s*$|\btail\s+-f\b", re.IGNORECASE | re.MULTILINE)

HANDOFF = ("Hand the job back instead: end now with a short report: what is done, the exact command to run (or, if it "
           "is already running in the background, its output / log path), and what to check when it finishes. The "
           "main session (1 h cache) runs or waits for it and starts a fresh agent with the result.")


def wait_seconds(tool, inp):
    """Seconds this call may block waiting (0 when it doesn't), why, and whether it is a plain job (not a wait)."""
    if tool in ("Bash", "PowerShell"):
        if inp.get("run_in_background"):
            return 0, "", False
        cmd = inp.get("command") or ""
        timeout_s = (inp.get("timeout") or 0) / 1000
        slept = 0.0
        for n, unit in SLEEP.findall(cmd):
            slept += float(n) * {"": 1, "s": 1, "m": 60, "h": 3600}[unit.lower()]
        slept += sum(float(n) / 1000 for n in START_SLEEP_MS.findall(cmd))
        slept += sum(float(n) for n in TIMEOUT_T.findall(cmd))
        deadline = max([float(n) * (60 if u.lower() == "minutes" else 1) for u, n in DEADLINE.findall(cmd)] or [0])
        if hand_rolled_wait(cmd):  # waits as long as another job runs, whatever its timeout says
            return (max(timeout_s, 3600), "a loop waiting for another job to finish (queue your own job with "
                    "`slot run` instead)", False)
        looping = bool(LOOP.search(cmd)) and (slept or deadline)
        if looping:  # a poll loop runs until its timeout / deadline, not for one sleep
            secs = max(deadline, timeout_s or 120)
            return secs, "a wait loop", False
        if BLOCKING.search(cmd):
            return max(timeout_s or 120, slept), "a blocking wait", False
        if slept:
            return slept, "a sleep", False
        if timeout_s > WAIT_LIMIT:
            return timeout_s, f"a foreground job with a {timeout_s / 60:.0f} min timeout", True
        return 0, "", False
    if tool in ("TaskOutput", "Monitor", "AgentOutput") or tool.endswith("Wait"):
        if inp.get("block") is False:
            return 0, "", False
        t = inp.get("timeout") or inp.get("timeout_ms") or 0
        secs = t / 1000 if t > 3600 else t
        return secs, f"a blocking {tool} wait", False
    return 0, "", False


def hand_rolled_wait(cmd):
    from devtime import slotrules
    return slotrules.hand_rolled_wait(cmd)


def agent_transcript(transcript_path, session_id, agent_id):
    base = os.path.join(os.path.dirname(transcript_path or ""), session_id or "", "subagents")
    direct = os.path.join(base, f"agent-{agent_id}.jsonl")
    if os.path.exists(direct):
        return direct
    found = glob.glob(os.path.join(base, "**", f"agent-{agent_id}.jsonl"), recursive=True)
    return found[0] if found else None


def context_tokens(path):
    """Context of the agent's latest request: input + cache read + cache write, from the transcript's tail."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            lines = handle.read().splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        usage = (rec.get("message") or {}).get("usage") or {}
        if usage:
            return (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) +
                    usage.get("cache_creation_input_tokens", 0))
    return 0


def load_state(agent_id):
    try:
        with open(os.path.join(STATE_DIR, f"{agent_id}.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"waited": 0.0, "nudged": 0, "slow": 0, "alerts": []}


def save_state(agent_id, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{agent_id}.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)
    if not int(time.time()) % 50:  # now and then, forget agents idle for over 3 days
        for old in glob.glob(os.path.join(STATE_DIR, "*.json")):
            try:
                if time.time() - os.path.getmtime(old) > 3 * 86400:
                    os.remove(old)
            except OSError:
                pass


def after(data, state, ctx):
    """PostToolUse: note calls that really ran past the cache's life; tell the agent the first time."""
    secs = (data.get("duration_ms") or 0) / 1000
    if secs < WAIT_LIMIT:
        return None
    state["slow"] = state.get("slow", 0) + 1
    state["waited"] += secs
    if state["slow"] > 1:
        return None
    size = f"~{ctx / 1000:.0f}k tokens" if ctx else "its whole context"
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
        f"Agent budget hook: that call ran {secs / 60:.0f} min. A subagent's prompt cache expires after 5 min, so the "
        f"next request re-writes {size} at cache-write price. Further long jobs in this agent will be refused. "
        f"{HANDOFF}")}}


MAIN_ONLY = {"big context", "read-only on a big model", "untagged coding stage"}


def pending_alerts(session_id, agent_id, state):
    """New usage alerts for this subagent (about itself) or this main session (about itself or its subagents), from
    the instance of this session's org only."""
    if not paths.ORG:
        return []
    try:
        with open(ALERTS, encoding="utf-8") as handle:
            items = json.load(handle).get("alerts", [])
    except (OSError, ValueError):
        return []
    now = time.time()
    done = set(state.get("alerts", []))
    out = []
    for a in items:
        if a["key"] in done or now - a.get("last", 0) > ALERT_FRESH_S or a.get("session") != session_id:
            continue
        if agent_id:
            if a.get("agent") != agent_id or a["kind"] in MAIN_ONLY:  # its own nudge / only the main session can act
                continue
        out.append(a)
    state["alerts"] = (state.get("alerts", []) + [a["key"] for a in out])[-200:]
    return out


def alert_text(items, agent_id):
    lines = ["devtime usage alert (automatic, from the transcripts' token counts):"]
    for a in items:
        who = "this agent" if agent_id else (f"your subagent '{a['owner']}' ({a['model']}, id {a['agent']})" if a.get("agent")
                                             else "this session")
        lines.append(f"  - {who} {a['text']}. Suggest: {a['advice']}.")
    lines.append("If it can't be made cheaper, carry on; it is only reported once per level. "
                 f"Details: {dashboard()}")
    return "\n".join(lines)


def with_alerts(out, items, agent_id, event):
    """Adds the alert text to the hook's output (a deny reason or extra context), or makes the output."""
    if not items:
        return out
    text = alert_text(items, agent_id)
    if out is None:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    spec = out["hookSpecificOutput"]
    field = "permissionDecisionReason" if "permissionDecisionReason" in spec else "additionalContext"
    spec[field] = spec[field] + "\n" + text
    return out


RUNNER_DESCRIPTION = re.compile(r"^\s*runner\b", re.IGNORECASE)


def is_runner(path):
    """A general-purpose agent spawned as a runner ("Runner (haiku): build ...") by a session that started before the
    runner agent type existed: its description is in the meta file next to its transcript."""
    if not path:
        return False
    try:
        with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
            return bool(RUNNER_DESCRIPTION.match(json.load(f).get("description") or ""))
    except (OSError, ValueError, AttributeError):
        return False


AB_TAG = re.compile(r"^AB:([\w-]+):([\w.-]+)", re.IGNORECASE)


def arm_index(exp_id, stage, n):
    """Same as devtime.ab.arm_index (duplicated: this hook runs on every tool call)."""
    import hashlib
    return int(hashlib.sha1((exp_id.lower() + ":" + stage.lower()).encode("utf-8")).hexdigest(), 16) % n


def ab_check(inp):
    """A deny output if an Agent launch tagged AB:<id>:<stage> doesn't match the stage's arm, else None."""
    m = AB_TAG.match(str(inp.get("description") or ""))
    if not m:
        return None
    import tomllib
    exp_id, stage = m.group(1).lower(), m.group(2).lower()
    try:
        with open(os.path.join(paths.USER_CONFIG, "experiments.toml"), "rb") as f:
            exps = tomllib.load(f).get("experiment") or []
    except (OSError, ValueError):
        return None
    exp = next((e for e in exps if str(e.get("id")).lower() == exp_id and e.get("active", True)), None)
    arms = [a for a in (exp or {}).get("arms") or [] if isinstance(a, dict) and a.get("name")]
    if not arms:
        return None
    arm = arms[arm_index(exp_id, stage, len(arms))]
    want_model, want_type = str(arm.get("model") or "").lower(), str(arm.get("agent_type") or "").lower()
    model, kind = str(inp.get("model") or "").lower(), str(inp.get("subagent_type") or "").lower()
    bad = []
    if want_model and not (model and (want_model in model or model in want_model)):
        bad.append("model %s (this launch: %s)" % (arm["model"], inp.get("model") or "not set, so inherited"))
    if want_type and not (kind == want_type or (":" not in kind or ":" not in want_type) and
                          kind.split(":")[-1] == want_type.split(":")[-1]):
        bad.append("subagent_type %s (this launch: %s)" % (arm["agent_type"], inp.get("subagent_type") or "not set"))
    if not bad:
        return None
    reason = ("A/B experiment %s: stage '%s' belongs to arm '%s', which needs %s. Relaunch with that%s; the arm is "
              "fixed by hash (`ab arm %s %s`), sessions don't choose it." %
              (exp_id, stage, arm["name"], " and ".join(bad),
               "; add to its brief: " + str(arm["instructions"]) if arm.get("instructions") else "", exp_id, stage))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def slot_check(data):
    """devtime slots (devtime/slotrules.py), every session: None, ("deny", reason) or ("wrap", new input, note)."""
    if (data.get("hook_event_name") or "PreToolUse") != "PreToolUse" or data.get("tool_name") not in ("Bash",
                                                                                                    "PowerShell"):
        return None
    if not on("slots"):
        return None
    try:
        from devtime import slotrules
        result = slotrules.hook_check(data["tool_name"], data.get("tool_input") or {}, data.get("cwd"),
                                      paths.launcher_path(), subagent=bool(data.get("agent_id")))
        if result and result[0] == "wrap":
            paths.install_launcher(ROOT)  # the stable launcher the wrapped command calls, current with this plugin
        return result
    except Exception:  # never let the queue break a call: leave it as it is
        return None


def decide(data):
    """The hook's JSON output for one event, or None to stay silent."""
    slot = slot_check(data)
    if slot and slot[0] == "deny":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": slot[1]}}
    out = budget(data)
    if slot and slot[0] == "wrap":
        if out and out["hookSpecificOutput"].get("permissionDecision") == "deny":
            return out
        spec = out["hookSpecificOutput"] if out else {"hookEventName": "PreToolUse"}
        spec["updatedInput"] = slot[1]  # no permissionDecision: the normal permission checks see the wrapped command
        spec["additionalContext"] = (spec.get("additionalContext", "") + "\n" + slot[2]).strip()
        return {"hookSpecificOutput": spec}
    return out


def budget(data):
    """The agent budget / alerts / A/B part of the hook's output, or None."""
    agent_id = data.get("agent_id")
    event = data.get("hook_event_name") or "PreToolUse"
    if event == "PreToolUse" and data.get("tool_name") in ("Agent", "Task"):
        denied = ab_check(data.get("tool_input") or {})
        if denied:
            return denied
    if not agent_id:  # main session: waits are cheap there (1 h cache); only usage alerts
        if event != "PreToolUse" or not data.get("session_id"):
            return None
        key = "main-" + data["session_id"]
        state = load_state(key)
        items = pending_alerts(data["session_id"], None, state)
        if items:
            save_state(key, state)
        return with_alerts(None, items, None, event)
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    state = load_state(agent_id)
    if not on("agent_budget"):  # no parallel support: no wait refusals, no handoff nudge; alerts only
        if event != "PreToolUse":
            return None
        out = with_alerts(None, pending_alerts(data.get("session_id"), agent_id, state), agent_id, event)
        save_state(agent_id, state)
        return out
    path = agent_transcript(data.get("transcript_path"), data.get("session_id"), agent_id)
    ctx = context_tokens(path) if path else 0
    waiter = data.get("agent_type") in WAITERS or is_runner(path)
    if data.get("hook_event_name") == "PostToolUse":
        out = None if waiter else after(data, state, ctx)
        save_state(agent_id, state)
        return out
    out = None

    secs, why, job = (0, "", False) if waiter else wait_seconds(tool, inp)
    if secs:
        if job and not state.get("slow"):
            pass  # a build / run that may well be quick: let it through; PostToolUse sees how long it really took
        elif secs > WAIT_LIMIT or state["waited"] + secs > WAIT_BUDGET:
            spent = f" (this agent has already waited {state['waited'] / 60:.0f} min)" if state["waited"] else ""
            size = f"~{ctx / 1000:.0f}k tokens" if ctx else "its whole context"
            reason = (f"Agent budget hook: subagents don't wait on slow jobs. This is {why} of up to "
                      f"{secs / 60:.0f} min{spent}; a subagent's prompt cache expires after 5 min, so waking up "
                      f"re-writes {size} at cache-write price. {HANDOFF}")
            out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": reason}}
        elif not job:
            state["waited"] += secs

    if out is None and ctx >= CONTEXT_NUDGE and ctx >= state["nudged"] + CONTEXT_STEP:
        state["nudged"] = ctx
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": (
            f"Agent budget hook: this subagent's context is ~{ctx / 1000:.0f}k tokens and every tool call re-reads "
            "all of it. Finish the current step (commit if it is code), then end with a short handoff (what is done, "
            "what is next, key paths and commands) so a fresh agent continues from a small context. Don't start a "
            "new stage in this agent.")}}
    out = with_alerts(out, pending_alerts(data.get("session_id"), agent_id, state), agent_id, event)
    save_state(agent_id, state)
    return out


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    out = decide(data)
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a hook must never break the session's tool calls
        pass
