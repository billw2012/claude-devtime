"""Incremental extraction of timing events from Claude Code transcripts.

Every transcript (a session's <id>.jsonl, or a subagent's agent-*.jsonl) is read once and boiled down to a short list of
events: API requests (timestamps, model, token usage, thinking time), tool calls and their results, background-task
notifications, human prompts, compactions and cost-state snapshots. The events are cached per file together with the
byte offset reached, so a transcript that grows is only read from where the last scan stopped.

Only what the dashboard needs is kept: tool names, short labels (shell descriptions, file basenames, subagent
descriptions) and the text used to classify shell commands. Prompts, tool outputs and file contents are never stored.
"""

import datetime
import glob
import json
import os
import pickle
import re
from concurrent.futures import ProcessPoolExecutor

from . import orgs, own

CACHE_VERSION = 9  # 7: FileState.org; 8: per-org instances, org tags of every transcript; 9: "cmd" events

SHELL_TOOLS = ("Bash", "PowerShell")
TASK_ID_RE = re.compile(r"<task-id>([^<\s]+)</task-id>")
TASK_TOOL_RE = re.compile(r"<tool-use-id>([^<\s]+)</tool-use-id>")
TASK_STATUS_RE = re.compile(r"<status>([^<\s]+)</status>")
BG_ID_RE = re.compile(r"running in background with ID: (\w+)")
OUTPUT_RE = re.compile(r"Output is being written to: (.+?\.output)")
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
MON_TAG_RE = re.compile(r"(?<![A-Za-z])MON:([\w.-]+)")
_ts_cache = {}


def parse_ts(text):
    value = _ts_cache.get(text)
    if value is None:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        if len(_ts_cache) > 200000:
            _ts_cache.clear()
        _ts_cache[text] = value
    return value


def tool_label(name, inp):
    """A short, content-free label for a tool call."""
    if name in SHELL_TOOLS:
        desc = str(inp.get("description") or "").strip()
        if desc:
            return desc[:120]
        return str(inp.get("command") or "").strip().split("\n", 1)[0][:80]
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        return os.path.basename(path.replace("\\", "/"))[:80]
    if name == "Agent":
        return str(inp.get("description") or "")[:120]
    if name == "Skill":
        return str(inp.get("skill") or "")[:80]
    if name == "WebFetch":
        m = re.match(r"https?://([^/]+)", str(inp.get("url") or ""))
        return m.group(1) if m else ""
    if name == "Workflow":
        return str(inp.get("name") or "workflow")[:80]
    if name == "ScheduleWakeup":
        return str(inp.get("reason") or "")[:100]
    return ""


def classify_text(name, inp):
    """What the classification rules look at: shell description + command, or the tool name for everything else."""
    if name in SHELL_TOOLS:
        return (str(inp.get("description") or "") + "\n" + str(inp.get("command") or ""))[:3000]
    if name.startswith("mcp__"):
        keys = ("description", "action", "command", "name", "tool")
        return " ".join(str(inp.get(k)) for k in keys if isinstance(inp.get(k), str))[:500]
    return ""


def notification_trigger(text):
    """What woke the session: "monitor:<name>" for a Monitor event (the name from the MON:<name> tag in the monitor's
    description / the event lines, "untagged" without one), "agent", "background-command" or "notification"."""
    first = None
    for block in text.split("<task-notification>")[1:]:
        m = SUMMARY_RE.search(block)
        summary = (m.group(1) if m else "").lstrip()
        if summary.startswith("Monitor"):
            tag = MON_TAG_RE.search(summary) or MON_TAG_RE.search(block)
            return "monitor:" + (tag.group(1) if tag else "untagged")
        if first is None:
            if summary.startswith(("Agent ", "Background agent")):
                first = "agent"
            elif summary.startswith("Background command"):
                first = "background-command"
            else:
                first = "notification"
    return first


def _usage(u):
    if not isinstance(u, dict):
        return None
    cc = u.get("cache_creation") or {}
    cw = u.get("cache_creation_input_tokens") or 0
    cw1h = cc.get("ephemeral_1h_input_tokens") or 0
    cw5m = cc.get("ephemeral_5m_input_tokens")
    if cw5m is None:
        cw5m = max(cw - cw1h, 0)
    think = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
    return (u.get("input_tokens") or 0, u.get("output_tokens") or 0, u.get("cache_read_input_tokens") or 0,
            cw5m, cw1h, think)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class FileState:
    """Cached extraction for one transcript file."""

    def __init__(self, path):
        self.path = path
        self.size = 0
        self.mtime = 0.0
        self.offset = 0
        self.prev_ts = None
        self.events = []
        self.cwds = {}
        self.branches = {}
        self.title = None
        self.org = None  # organisation uuid from the transcript's own credential_org record
        self.session_id = None
        self.agent_id = None
        self.entrypoint = None
        self.version = None
        self.first_ts = None
        self.last_ts = None
        self.cost_states = []
        self.models = {}
        self.pending = []  # (queued time, trigger) of messages queued mid-turn, delivered at the next record

    # ---- incremental read ----
    def update(self):
        st = os.stat(self.path)
        if st.st_size < self.offset:  # rewritten: start over
            self.__init__(self.path)
        if st.st_size == self.offset:
            self.size, self.mtime = st.st_size, st.st_mtime
            return False
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read()
        end = data.rfind(b"\n")
        if end < 0:
            return False
        chunk = data[:end + 1]
        self.offset += len(chunk)
        self.size, self.mtime = st.st_size, st.st_mtime
        for raw in chunk.split(b"\n"):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                self._record(record)
        return True

    def _record(self, r):
        rtype = r.get("type")
        if rtype == "attachment":
            self.org = orgs.tag_of(r) or self.org
        ts_text = r.get("timestamp")
        ts = parse_ts(ts_text) if isinstance(ts_text, str) else None
        if rtype in ("user", "assistant", "system") and ts is not None:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
            cwd = r.get("cwd")
            if cwd:
                self.cwds[cwd] = self.cwds.get(cwd, 0) + 1
            br = r.get("gitBranch")
            if br:
                self.branches[br] = self.branches.get(br, 0) + 1
            self.session_id = self.session_id or r.get("sessionId")
            self.agent_id = self.agent_id or r.get("agentId")
            self.entrypoint = self.entrypoint or r.get("entrypoint")
            self.version = r.get("version") or self.version
        branch = r.get("gitBranch") or ""
        queued = rtype == "attachment" and isinstance(r.get("attachment"), dict)             and r["attachment"].get("type") == "queued_command"
        if self.pending and ts is not None and not queued and rtype in ("user", "assistant", "attachment", "system"):
            # A message queued during a turn is logged at its queue time but reaches the model when the turn's next
            # record is written: that record's time is when it woke the model.
            for queued_at, trigger in self.pending:
                self.events.append(("tg", max(ts, queued_at), trigger))
            self.pending = []

        if rtype == "assistant" and ts is not None:
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            rid = r.get("requestId") or msg.get("id") or r.get("uuid")
            model = msg.get("model") or ""
            if model and model != "<synthetic>":
                self.models[model] = self.models.get(model, 0) + 1
            prev = self.prev_ts if self.prev_ts is not None else ts
            self.events.append(("a", ts, prev, rid, msg.get("id"), model, _usage(msg.get("usage")),
                                r.get("thinkingDurationMs") or 0, branch))
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name = b.get("name") or ""
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    extra = None
                    if name == "Agent":
                        extra = (str(inp.get("subagent_type") or ""), bool(inp.get("run_in_background")))
                    elif name in SHELL_TOOLS:
                        extra = inp.get("timeout") if isinstance(inp.get("timeout"), (int, float)) else None
                    elif name == "TaskStop":
                        extra = str(inp.get("task_id") or inp.get("shell_id") or "")
                    self.events.append(("tu", ts, b.get("id"), name, classify_text(name, inp), tool_label(name, inp),
                                        bool(inp.get("run_in_background")), extra, branch))
            self.prev_ts = ts
        elif rtype == "user" and ts is not None:
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            content = msg.get("content")
            origin = r.get("origin") if isinstance(r.get("origin"), dict) else {}
            okind = origin.get("kind")
            has_result = False
            tur = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        has_result = True
                        bg = tur.get("backgroundTaskId")
                        output = None
                        res = b.get("content")
                        res_text = (res if isinstance(res, str) else _text_of(res))[:2000]
                        if not bg:
                            m = BG_ID_RE.search(res_text)
                            bg = m.group(1) if m else None
                        if bg:
                            m = OUTPUT_RE.search(res_text)
                            output = m.group(1) if m else None
                        agent_id = tur.get("agentId") if tur.get("isAsync") or tur.get("status") else None
                        self.events.append(("tr", ts, b.get("tool_use_id"), bg, bool(b.get("is_error")),
                                            agent_id or tur.get("runId"), output))
            if not has_result:
                text = _text_of(content)
                trigger = None
                if "<task-id>" in text:
                    self._notifications(text, ts)
                    okind = okind or "task-notification"
                    trigger = notification_trigger(text)
                elif okind == "human" or (okind is None and not r.get("isMeta")):
                    trigger = "human"
                elif okind == "peer":
                    trigger = "peer"
                if trigger:
                    self.events.append(("tg", ts, trigger))
                cmd = own.command_name(text) if trigger == "human" else None
                if cmd:  # only the name of a devtime skill, never the text around it
                    self.events.append(("cmd", ts, cmd))
                if not r.get("isMeta") or okind:
                    self.events.append(("u", ts, okind or ("human" if not r.get("isMeta") else "meta")))
            self.prev_ts = ts
        elif rtype == "queue-operation" and ts is not None:
            text = r.get("content")
            if isinstance(text, str) and r.get("operation") == "enqueue" and "<task-id>" in text:
                self._notifications(text, ts)
        elif rtype == "attachment" and ts is not None:
            att = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
            if att.get("type") == "queued_command":  # how subagents receive their task-notifications
                text = att.get("prompt")
                if isinstance(text, str) and "<task-id>" in text:
                    self._notifications(text, ts)
                    self.pending.append((ts, notification_trigger(text)))
                else:
                    okind = (att.get("origin") or {}).get("kind") if isinstance(att.get("origin"), dict) else None
                    if okind in ("human", "peer"):
                        self.pending.append((ts, okind))
        elif rtype == "system" and ts is not None:
            if r.get("subtype") == "compact_boundary":
                meta = r.get("compactMetadata") or {}
                self.events.append(("cb", ts, float(meta.get("durationMs") or 0) / 1000.0, meta.get("trigger")))
            self.prev_ts = ts
        elif rtype == "custom-title":
            self.title = r.get("customTitle") or self.title
        elif rtype == "cost-state":
            keep = {k: r.get(k) for k in ("totalCostUSD", "totalAPIDuration", "totalToolDuration", "totalDuration",
                                          "startTime", "totalLinesAdded", "totalLinesRemoved", "modelUsage")}
            keep["seenAt"] = self.last_ts
            self.cost_states.append(keep)

    def _notifications(self, text, ts):
        for block in text.split("<task-notification>")[1:]:
            tid = TASK_ID_RE.search(block)
            if not tid:
                continue
            tool = TASK_TOOL_RE.search(block)
            status = TASK_STATUS_RE.search(block)
            self.events.append(("tn", ts, tid.group(1), tool.group(1) if tool else None,
                                status.group(1) if status else None))


# ---- discovery ----

def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def discover(projects_dir, extra_globs=()):
    """Yields dicts describing every transcript: main sessions, their subagents and workflow agents."""
    out = []
    folders = sorted(glob.glob(os.path.join(projects_dir, "*")))
    for pattern in extra_globs:
        folders.extend(sorted(glob.glob(os.path.expanduser(pattern))))
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for session in glob.glob(os.path.join(folder, "*.jsonl")):
            sid = os.path.splitext(os.path.basename(session))[0]
            sdir = os.path.join(folder, sid)
            title = (read_json(os.path.join(sdir, "custom-title.json")) or {}).get("customTitle")
            out.append({"path": session, "role": "main", "session": sid, "folder": os.path.basename(folder),
                        "title": title})
            for agent in glob.glob(os.path.join(sdir, "subagents", "agent-*.jsonl")):
                meta = read_json(agent[:-6] + ".meta.json") or {}
                out.append({"path": agent, "role": "subagent", "session": sid, "folder": os.path.basename(folder),
                            "agent": os.path.basename(agent)[6:-6], "meta": _meta(meta)})
            for agent in glob.glob(os.path.join(sdir, "subagents", "workflows", "*", "agent-*.jsonl")):
                meta = read_json(agent[:-6] + ".meta.json") or {}
                wf = os.path.basename(os.path.dirname(agent))
                out.append({"path": agent, "role": "workflow", "session": sid, "folder": os.path.basename(folder),
                            "agent": os.path.basename(agent)[6:-6], "workflow": wf, "meta": _meta(meta)})
    return out


def _meta(meta):
    return {k: meta.get(k) for k in ("agentType", "description", "toolUseId", "model", "requestShape", "spawnDepth",
                                     "workflowPhase")}


def desktop_sessions(app_dirs):
    """Titles and worktree info the Claude desktop app keeps per session (keyed by CLI session id)."""
    found = {}
    for base in app_dirs:
        for path in glob.glob(os.path.join(os.path.expanduser(base), "*", "*", "local_*.json")):
            d = read_json(path)
            if not isinstance(d, dict) or not d.get("cliSessionId"):
                continue
            found[d["cliSessionId"]] = {"title": d.get("title"), "cwd": d.get("cwd"), "originCwd": d.get("originCwd"),
                                        "branch": d.get("branch"), "archived": d.get("isArchived"),
                                        "spawnedFrom": (d.get("spawnedFrom") or {}).get("title")}
    return found


# ---- cache ----

class Cache:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.path = os.path.join(cache_dir, "transcripts.pickle")
        self.states = {}  # only transcripts of this instance's org(s)
        self.tags = {}  # every transcript: (offset searched, org tag or None), see orgs.scan_tag
        if os.path.exists(self.path):
            try:
                with open(self.path, "rb") as handle:
                    version, states, tags = pickle.load(handle)
                if version == CACHE_VERSION:
                    self.states, self.tags = states, tags
            except Exception:
                self.states, self.tags = {}, {}

    def save(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as handle:
            pickle.dump((CACHE_VERSION, self.states, self.tags), handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self.path)

    def orgs_of(self, discovered, index):
        """{path: org} of every discovered transcript (orgs.classify); forgets the tags of files that are gone."""
        found = orgs.classify(discovered, self.tags, index)
        for path in list(self.tags):
            if path not in found:
                del self.tags[path]
        return found

    def refresh(self, paths, workers=None):
        """Brings every path up to date (and drops every other file's state); returns the number of files that
        changed. Only the paths of this instance's org(s) are passed in."""
        stale = []
        for path in paths:
            state = self.states.get(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if state is None or st.st_size != state.offset or st.st_mtime != state.mtime:
                stale.append(path)
        for path in list(self.states):
            if path not in paths:
                del self.states[path]
        if not stale:
            return 0
        # Big first scans go to a process pool; small increments are faster inline.
        new_bytes = sum(os.path.getsize(p) - (self.states[p].offset if p in self.states else 0) for p in stale)
        if new_bytes > 64 * 1024 * 1024 and len(stale) > 4:
            todo = [self.states.get(p) or FileState(p) for p in stale]
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for state in pool.map(_update_state, todo, chunksize=4):
                    self.states[state.path] = state
        else:
            for path in stale:
                state = self.states.get(path) or FileState(path)
                state.update()
                self.states[path] = state
        return len(stale)


def _update_state(state):
    state.update()
    return state
