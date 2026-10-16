"""Which organisation a transcript belongs to (one account can be signed into several in the Claude desktop app).

All organisations write to the same ~/.claude/projects, so DevTime separates them itself: each org's instance
(paths.py) only reads the transcripts of its own org; of the others it reads no more than the org tag. A transcript's
org comes from, in this order:

1. its own `credential_org` record: {"type": "attachment", "attachment": {"type": "credential_org",
   "organizationUuid": "<uuid>"}} (written by newer Claude Code versions, often several times per transcript);
2. the desktop app's session index: <desktop dir>/claude-code-sessions/<account>/<org>/local_<id>.json (and
   local-agent-mode-sessions/<account>/<org>/...), each with "cliSessionId" = the transcript's session id;
3. its parent session's org (subagents and workflow agents);
4. "unknown".

The desktop dir is settings.desktop_dir / env DEVTIME_DESKTOP_DIR, else the app's folder for the OS. Only ids and org
folder names are read from those files. Which org the current process runs in is paths.current_org(), not this.
"""

import glob
import json
import os
import sys

UNKNOWN = "unknown"
_file_cache = {}  # path -> ((mtime, size), session id or None)


def desktop_base(settings=None):
    value = os.environ.get("DEVTIME_DESKTOP_DIR") or (settings or {}).get("desktop_dir")
    if value:
        return os.path.expanduser(str(value))
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming"), "Claude")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    return os.path.expanduser("~/.config/Claude")


def _session_of(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_mtime, st.st_size)
    hit = _file_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        sid = d.get("cliSessionId") if isinstance(d, dict) else None
    except (OSError, ValueError):
        sid = None
    _file_cache[path] = (sig, sid if isinstance(sid, str) and sid else None)
    return _file_cache[path][1]


def desktop_index(settings=None):
    """{CLI session id: org uuid} from the desktop app's session folders (missing folders are skipped)."""
    base = desktop_base(settings)
    found = {}
    for folder in ("claude-code-sessions", "local-agent-mode-sessions"):
        for path in glob.glob(os.path.join(base, folder, "*", "*", "*.json")):
            parts = path.replace("\\", "/").split("/")
            if "skills-plugin" in parts:
                continue
            sid = _session_of(path)
            if sid:
                found[sid] = parts[-2]
    return found


def tag_of(record):
    """The org uuid of a credential_org record (a parsed transcript line), else None."""
    att = record.get("attachment") if isinstance(record, dict) else None
    if isinstance(att, dict) and att.get("type") == "credential_org":
        org = att.get("organizationUuid")
        if isinstance(org, str) and org:
            return org
    return None


def read_tag(path, max_lines=400):
    """The org tag of a transcript file, reading only its first lines (None if it has none)."""
    org = None
    try:
        with open(path, "rb") as f:
            for n, raw in enumerate(f):
                if n >= max_lines:
                    break
                if b"credential_org" in raw:
                    try:
                        org = tag_of(json.loads(raw)) or org
                    except ValueError:
                        pass
    except OSError:
        pass
    return org


def resolve(tag, session, index, parent_org=None):
    """The org of a main session: its tag, else the desktop index, else (a subagent) its parent's."""
    return tag or (index or {}).get(session) or parent_org or UNKNOWN


def matches(org, spec):
    """True if the org uuid `org` is what `spec` (a uuid, or a short prefix of one) names."""
    spec = str(spec or "")
    return bool(spec) and (org == spec or (spec != UNKNOWN and org != UNKNOWN and len(spec) >= 4
                                           and org.startswith(spec)))


def scan_tag(path, prev=None):
    """(offset, tag): the first org tag of a transcript, read incrementally. `prev` is the (offset, tag) of an earlier
    call: a tag once found is kept; otherwise only the bytes past the offset are searched. Only lines that contain
    "credential_org" are parsed; nothing else of the file is kept."""
    offset, tag = prev or (0, None)
    try:
        if os.path.getsize(path) < offset:  # rewritten: start over
            offset, tag = 0, None
    except OSError:
        return offset, tag
    if tag:
        return offset, tag
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written
                offset += len(raw)
                if b"credential_org" in raw:
                    try:
                        tag = tag_of(json.loads(raw))
                    except ValueError:
                        tag = None
                    if tag:
                        break
    except OSError:
        pass
    return offset, tag


def classify(discovered, tags, index):
    """{path: org} for discovered transcripts (scan.discover entries). tags: {path: (offset, tag)}, updated in place
    (scan_tag); index: desktop_index(). A main session: its tag, else the desktop index, else "unknown"; a subagent /
    workflow agent: its own tag, else its main session's org."""
    for d in discovered:
        tags[d["path"]] = scan_tag(d["path"], tags.get(d["path"]))
    out, mains = {}, {}
    for d in discovered:
        if d["role"] == "main":
            out[d["path"]] = mains[(d["folder"], d["session"])] = resolve(tags[d["path"]][1], d["session"], index)
    for d in discovered:
        if d["role"] != "main":
            parent = mains.get((d["folder"], d["session"])) or resolve(None, d["session"], index)
            out[d["path"]] = tags[d["path"]][1] or parent
    return out
