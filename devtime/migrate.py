"""One-time move of the pre-0.4.0 cache (DATA/cache, shared by every org) into the per-org instances (paths.py).

What can't be rebuilt goes to the instance of the org it belongs to; nothing of one org lands in another's:

    plan_samples.jsonl, review_notes.jsonl   by each row's "org" (a full id, or a prefix naming exactly one known org);
                                             rows without an org (or "unknown") only on a single-org machine
    work_sections.json                       by the org of each labelled transcript (agent / session id)
    review-log.md, review_state.json         no org recorded: only on a single-org machine

"Known orgs" are the orgs of every transcript on the machine (their org tags, the desktop app's index). Everything else
(transcript caches, alerts, the usage harvest, Unreal caches, logs, port.txt) is rebuilt by each instance and dropped.
The old folder is removed afterwards; rows are de-duplicated, so a re-run (an old server still writing there) is safe.

Run by `devtime tick` and `devtime serve` before they use the instance.
"""

import json
import os
import shutil
import time

from . import orgs, paths, scan

LOCK_S = 600


def _known_orgs(settings):
    """({transcript id: org}, {known org ids}) of every transcript under the projects dirs."""
    discovered = scan.discover(os.path.expanduser(settings.get("projects_dir", "~/.claude/projects")),
                               settings.get("extra_project_dirs", []))
    index = orgs.desktop_index(settings)
    org_of = orgs.classify(discovered, {}, index)
    ids = {}
    for d in discovered:
        ids[d.get("agent") or d["session"]] = org_of[d["path"]]
    known = {o for o in org_of.values() if o != orgs.UNKNOWN} | set(index.values())
    return ids, known


def _owner(org, known, single):
    """The full org id a row recorded for `org` belongs to, or None (drop it)."""
    if not org or org == orgs.UNKNOWN:
        return single
    if org in known:
        return org
    hits = [k for k in known if orgs.matches(k, org)]
    if len(hits) == 1:
        return hits[0]
    return org if len(org) >= 32 and not hits else None  # a full id of an org whose transcripts are gone: its own


def _read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []


def _append_unique(path, lines):
    have = set(_read_lines(path))
    new = [line for line in lines if line not in have]
    if new:
        paths.ensure(os.path.dirname(path))
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in new))


def _dest(org, name):
    return os.path.join(paths.instance_of(org), "cache", name)


def migrate(legacy, settings):
    """Splits the legacy cache folder into the instances; returns {org: [file names written]}."""
    ids, known = _known_orgs(settings)
    single = next(iter(known)) if len(known) == 1 else None
    written = {}

    def note(org, name):
        written.setdefault(org, [])
        if name not in written[org]:
            written[org].append(name)

    for name in ("plan_samples.jsonl", "review_notes.jsonl"):
        groups = {}
        for line in _read_lines(os.path.join(legacy, name)):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            owner = _owner(row.get("org") if isinstance(row, dict) else None, known, single)
            if owner:
                row["org"] = owner
                groups.setdefault(owner, []).append(json.dumps(row))
        for owner, lines in groups.items():
            _append_unique(_dest(owner, name), lines)
            note(owner, name)
    try:
        with open(os.path.join(legacy, "work_sections.json"), encoding="utf-8") as f:
            assign = json.load(f).get("assign") or {}
    except (OSError, ValueError, AttributeError):
        assign = {}
    groups = {}
    for tid, section in assign.items() if isinstance(assign, dict) else ():
        owner = ids.get(tid)
        owner = single if owner in (None, orgs.UNKNOWN) else owner
        if owner:
            groups.setdefault(owner, {})[tid] = section
    for owner, part in groups.items():
        from .overview import work_sections
        path = _dest(owner, "work_sections.json")
        merged = dict(part, **work_sections(path))  # what the instance already has wins
        paths.ensure(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"assign": merged}, f, indent=0)
        note(owner, "work_sections.json")
    if single:
        for name in ("review-log.md", "review_state.json"):
            src, dst = os.path.join(legacy, name), _dest(single, name)
            if os.path.exists(src) and not os.path.exists(dst):
                paths.ensure(os.path.dirname(dst))
                shutil.copyfile(src, dst)
                note(single, name)
    return written


def run(settings=None):
    """Migrates DATA/cache if it exists (once; a lock keeps two processes from doing it at the same time). Returns
    what was written, or None when there was nothing to do. Never raises."""
    legacy = paths.LEGACY_CACHE
    if not os.path.isdir(legacy):
        return None
    lock = os.path.join(paths.DATA, "migrating.lock")
    try:
        if time.time() - os.path.getmtime(lock) < LOCK_S:
            return None
    except OSError:
        pass
    try:
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        if settings is None:
            from .config import Config
            settings = Config().settings
        written = migrate(legacy, settings)
        shutil.rmtree(legacy, ignore_errors=True)
        return written
    except Exception:
        return None
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
