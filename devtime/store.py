"""Keeps the dataset current: rescans transcripts incrementally and rebuilds when anything (or the config) changed."""

import os
import pickle
import threading
import time

from . import alerts, analyze, scan, unreal
from .config import Config


class Store:
    def __init__(self, root=None, verbose=True):
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
        self.save_every = 60.0
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
            changed = self.cache.refresh({d["path"] for d in discovered})
            try:
                force |= self.harvester.refresh()
            except Exception as error:  # a bad log must not stop the dashboard
                self.log("unreal logs:", repr(error))
            t1 = time.time()
            if changed or force or self.data is None:
                if changed and time.time() - self.last_save > self.save_every:
                    self.cache.save()  # the cache only saves re-reading; losing a minute of it costs nothing
                    self.last_save = time.time()
                    save_blocks = True
                else:
                    save_blocks = force and time.time() - self.last_save > self.save_every
                desktop = scan.desktop_sessions(s.get("desktop_session_dirs", []))
                builder = analyze.Builder(self.config, self.cache, discovered, desktop, self.blocks,
                                       self.harvester.runs)
                self.data = builder.build()
                self.blocks = builder.blocks
                if save_blocks or not os.path.exists(self._blocks_path()):
                    self._save_blocks()
                self.data["projects"] = analyze.projects_summary(self.data)
                self.data["defaultProject"] = analyze.default_project(self.data, s.get("default_project"))
                self.version += 1
                self.data["version"] = self.version
                try:
                    self.alerts.update(self.data, self.config)
                except Exception as error:  # alerts must not stop the dashboard
                    self.log("alerts:", repr(error))
                self.slices = {}
                self.log(f"scan {changed} changed files in {t1 - t0:.1f}s, build {time.time() - t1:.1f}s, "
                         f"{len(self.data['transcripts'])} transcripts, {len(self.data['iv']['t'])} intervals")
                self.last_scan = time.time()
                return True
            self.last_scan = time.time()
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

    def slice(self, project):
        key = project or "*"
        with self.lock:
            data, version = self.data, self.version
            cached = self.slices.get(key)
            if cached and cached[0] == version:
                return cached[1]
        out = analyze.subset(data, None if key == "*" else key)
        with self.lock:
            if self.version == version:
                self.slices[key] = (version, out)
        return out
