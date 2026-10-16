"""Keeps the dataset current: rescans transcripts incrementally and rebuilds when anything (or the config) changed.

The dataset is this instance's org only (paths.ORG, plus untagged transcripts with include_unknown_org): transcripts of
other orgs are classified by their org tag / the desktop index and then skipped, never scanned or cached."""

import glob
import os
import pickle
import threading
import time

from . import alerts, analyze, orgs, paths, readings, scan, unreal
from .config import Config


def overview_samples():
    from . import overview
    return overview.PLAN_SAMPLES


class Store:
    def __init__(self, root=None, verbose=True, plan_samples_path=None):
        self.config = Config(root) if root else Config()
        self.cache = scan.Cache(self.config.path("cache_dir"))
        self.lock = threading.Lock()
        self.data = None
        self.version = 0
        self.slices = {}
        self.verbose = verbose
        self.last_scan = None
        self.blocks = self._load_blocks()  # per-transcript rows, so a restart does not re-classify everything
        self.harvester = unreal.Harvester(self.config)
        self.last_save = 0.0
        self.desktop_orgs = {}
        self.kept = frozenset()  # paths of the transcripts in the dataset
        self.single_org = False  # every org seen on this machine is ours
        self.save_every = 60.0
        self.readings = readings.Readings(plan_samples_path or overview_samples())
        self.alerts = alerts.Alerts(os.path.join(self.config.path("cache_dir"), "alerts.json"))

    def log(self, *args):
        if self.verbose:
            print(time.strftime("%H:%M:%S"), *args, flush=True)

    def refresh(self, force=False):
        """Rescans; rebuilds the dataset if transcripts or config changed. Returns True when rebuilt."""
        with self.lock:
            t0 = time.time()
            try:
                if self.config.stamp() != self.config.mtime:
                    self.config = Config(self.config.root)
                    self.harvester.config = self.config
                    force = True
                    self.log("config reloaded")
            except Exception as error:  # a half-saved config keeps the previous one
                self.log("config error:", error)
            s = self.config.settings
            discovered = scan.discover(os.path.expanduser(s.get("projects_dir", "~/.claude/projects")),
                                       s.get("extra_project_dirs", []))
            desktop_orgs = orgs.desktop_index(s)
            allowed = paths.allowed_orgs(s)
            org_of = self.cache.orgs_of(discovered, desktop_orgs)
            discovered = [dict(d, org=org_of[d["path"]]) for d in discovered if org_of[d["path"]] in allowed]
            known = {o for o in org_of.values() if o != orgs.UNKNOWN} | set(desktop_orgs.values())
            single = bool(paths.ORG) and known <= {paths.ORG}
            kept = frozenset(d["path"] for d in discovered)
            if kept != self.kept or single != self.single_org:  # an org became known, or the settings changed
                force = True
                self.kept, self.single_org = kept, single
            changed = self.cache.refresh(kept)
            try:
                force |= self.harvester.refresh()
            except Exception as error:  # a bad log must not stop the dashboard
                self.log("unreal logs:", repr(error))
            if desktop_orgs != self.desktop_orgs:  # a session's org became known (or changed)
                force = True
            t1 = time.time()
            if changed or force or self.data is None:
                if changed and time.time() - self.last_save > self.save_every:
                    self.cache.save()  # the cache only saves re-reading; losing a minute of it costs nothing
                    self.last_save = time.time()
                    save_blocks = True
                else:
                    save_blocks = force and time.time() - self.last_save > self.save_every
                desktop = scan.desktop_sessions(s.get("desktop_session_dirs", []))
                self.desktop_orgs = desktop_orgs
                builder = analyze.Builder(self.config, self.cache, discovered, desktop, self.blocks,
                                          self.harvester.runs, desktop_orgs)
                self.data = builder.build()
                self.blocks = builder.blocks
                if save_blocks or not os.path.exists(self._blocks_path()):
                    self._save_blocks()
                self.data["projects"] = analyze.projects_summary(self.data)
                self.data["defaultProject"] = analyze.default_project(self.data, s.get("default_project"))
                self.version += 1
                self.data["version"] = self.version
                self.data["org"] = paths.ORG
                self.data["singleOrg"] = self.single_org
                try:
                    self.alerts.update(self.data, self.config)
                except Exception as error:  # alerts must not stop the dashboard
                    self.log("alerts:", repr(error))
                self.slices = {}
                self.log(f"scan {changed} changed files in {t1 - t0:.1f}s, build {time.time() - t1:.1f}s, "
                         f"{len(self.data['transcripts'])} transcripts, {len(self.data['iv']['t'])} intervals")
                self.last_scan = time.time()
                self._flush_readings()
                return True
            self.last_scan = time.time()
            self._flush_readings()
            return False

    def _blocks_path(self):
        return os.path.join(self.config.path("cache_dir"), "rows.pickle")

    def _load_blocks(self):
        try:
            with open(self._blocks_path(), "rb") as handle:
                return pickle.load(handle)
        except Exception:
            return {}

    def _save_blocks(self):
        tmp = self._blocks_path() + ".tmp"
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as handle:
            pickle.dump(self.blocks, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self._blocks_path())

    def slice(self, project="*"):
        """The dataset of one project ("*" = all). Unreal runs are kept only when linked to one of its transcripts."""
        key = project or "*"
        with self.lock:
            data, version = self.data, self.version
            cached = self.slices.get(key)
            if cached and cached[0] == version:
                return cached[1]
        if key == "*":
            out = analyze.subset(data, None, paths.allowed_orgs(self.config.settings))
            out["projects"] = analyze.projects_summary(out)
            out["defaultProject"] = analyze.default_project(out, self.config.settings.get("default_project"))
        else:
            out = analyze.subset(self.slice("*"), key)
        with self.lock:
            if self.version == version:
                self.slices[key] = (version, out)
        return out

    def org_of_session(self, session_id):
        """The org of a CLI session id (its transcript's, else the desktop index's), or None if unknown."""
        for t in (self.data or {}).get("transcripts", []):
            if t["role"] == "main" and t.get("session") == session_id and t.get("org") != orgs.UNKNOWN:
                return t["org"]
        return orgs.desktop_index(self.config.settings).get(session_id)

    # ---- readings reported by sessions ----
    def _flush_readings(self):
        if self.readings.pending:
            try:
                self.readings.flush(self.session_org, paths.ORG)
            except Exception as error:  # a bad queue entry must not stop the scan
                self.log("readings:", repr(error))

    def session_org(self, session_id):
        """The org of a session id: the scanned transcripts, the desktop index, else the transcript file's own
        credential_org tag (a session that started since the last scan). None if unknown."""
        found = self.org_of_session(session_id)
        if found:
            return found
        s = self.config.settings
        base = os.path.expanduser(s.get("projects_dir", "~/.claude/projects"))
        if not session_id or any(c in session_id for c in "/\\*?[]") or ".." in session_id:
            return None
        for path in glob.glob(os.path.join(glob.escape(base), "*", glob.escape(session_id) + ".jsonl")):
            tag = orgs.scan_tag(path, self.cache.tags.get(path))[1]
            if tag:
                return tag
        return None

    def is_ours(self, session_id):
        """True only if the session is provably of this instance's org (its transcript's tag or the desktop index)."""
        return bool(paths.ORG) and self.session_org(session_id) == paths.ORG

    def record_reading(self, body):
        """Stores (or queues) a reading posted by a session. Only readings of this instance's org are kept: the
        posting session's org (its transcript's tag or the desktop index) must be ours, and so must the org the
        module says it runs in, if it says. A session whose org is not known yet is queued; one that stays unknown
        is dropped. Returns the outcome word, or None if the body is no reading."""
        reading = readings.parse(body)
        if reading is None:
            return None
        claimed = body.get("org")
        if not paths.ORG or (claimed is not None and claimed != paths.ORG):
            return "ignored"
        org = self.session_org(reading["session"])
        if org is not None and org != paths.ORG:
            return "ignored"
        outcome = self.readings.add(reading, org)
        self._flush_readings()
        return outcome
