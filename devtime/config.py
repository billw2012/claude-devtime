"""Loads the configuration.

Shipped defaults: <plugin>/config/devtime.toml (settings, pricing, shared rules). Per-user files in the data directory
(devtime.paths.USER_CONFIG): devtime.toml overrides them (tables are merged key by key, [[rules]] / [[pre_rules]] of the
user file come first), and projects/*.toml are the project profiles (<plugin>/config/projects/*.toml too, if present).
"""

import fnmatch
import glob
import os
import re
import tomllib

from . import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL_TOOL_RE = re.compile(r"^(Bash|PowerShell)$")


FEATURES = ("slots", "agent_budget")  # settings.features.<name> overrides settings.parallel per feature
_USER_SETTINGS = {}  # user file -> ((mtime_ns, size), [settings])


def user_settings():
    """The [settings] table of the user's devtime.toml ({} if missing or unreadable), cached by the file's mtime: the
    PreToolUse hook asks on every tool call."""
    path = os.path.join(paths.USER_CONFIG, "devtime.toml")
    try:
        st = os.stat(path)
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _USER_SETTINGS.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, "rb") as handle:
            settings = tomllib.load(handle).get("settings")
    except (OSError, ValueError):
        settings = None
    settings = settings if isinstance(settings, dict) else {}
    _USER_SETTINGS[path] = (stamp, settings)
    return settings


def parallel():
    """settings.parallel: support for several sessions / autonomous pipelines on this PC (default off). It only adds
    features (the slot queue, the agent budget rules); nothing else depends on it."""
    return user_settings().get("parallel") is True


def feature(name):
    """Whether a parallel-support feature ("slots", "agent_budget") is on: settings.features.<name> if set (true /
    false), else settings.parallel."""
    override = user_settings().get("features")
    if isinstance(override, dict) and isinstance(override.get(name), bool):
        return override[name]
    return parallel()


class Rule:
    def __init__(self, spec):
        self.kind = spec["kind"]
        self.tool = re.compile(spec["tool"]) if spec.get("tool") else SHELL_TOOL_RE
        self.text = re.compile(spec["text"], re.IGNORECASE | re.MULTILINE) if spec.get("text") else None

    def match(self, name, text):
        return bool(self.tool.search(name)) and (self.text is None or bool(self.text.search(text)))


class Workstream:
    FIELDS = ("description", "session", "branch", "cwd")

    def __init__(self, spec):
        self.name = spec["name"]
        self.tests = [(f, re.compile(spec[f], re.IGNORECASE)) for f in self.FIELDS if spec.get(f)]

    def match(self, fields):
        return any(rx.search(fields.get(f) or "") for f, rx in self.tests)


def norm_path(path):
    """Lower-case, forward slashes, /d/... -> d:/..., no trailing slash."""
    p = (path or "").replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^/([a-z])/(.*)", p)
    if m:
        p = f"{m.group(1)}:/{m.group(2)}"
    return p


class Project:
    def __init__(self, spec, source):
        self.name = spec["name"]
        self.source = source
        self.paths = [norm_path(p) for p in spec.get("paths", [])]
        self.rules = [Rule(r) for r in spec.get("rules", [])]
        self.workstreams = [Workstream(w) for w in spec.get("workstreams", [])]
        self.unreal = spec.get("unreal")

    def owns(self, cwd):
        p = norm_path(cwd)
        return any(fnmatch.fnmatchcase(p, pat) for pat in self.paths)


def merge(base, over):
    """over on top of base: tables merge key by key (recursively), everything else is replaced, except that the rule
    lists of over come before those of base."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        elif key in ("rules", "pre_rules") and isinstance(out.get(key), list):
            out[key] = list(value) + out[key]
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, root=ROOT, user_dir=None):
        self.root = root
        self.user_dir = paths.USER_CONFIG if user_dir is None else user_dir
        with open(os.path.join(root, "config", "devtime.toml"), "rb") as handle:
            base = tomllib.load(handle)
        if os.path.exists(self.user_file()):
            with open(self.user_file(), "rb") as handle:
                base = merge(base, tomllib.load(handle))
        self.settings = base.get("settings", {})
        self.pricing = sorted(base.get("pricing", {}).items(), key=lambda kv: -len(kv[0]))
        self.waiting = set(base.get("kinds", {}).get("waiting", []))
        self.tools = base.get("tools", {})
        self.rules = [Rule(r) for r in base.get("rules", [])]
        self.pre_rules = [Rule(r) for r in base.get("pre_rules", [])]
        self.unreal = base.get("unreal", {})
        self.projects = []
        seen = set()
        for path in self.project_files():
            with open(path, "rb") as handle:
                project = Project(tomllib.load(handle), path)
            if project.name not in seen:  # the user's profile of a name wins over a shipped one
                seen.add(project.name)
                self.projects.append(project)
        self._kind_cache = {}
        self.mtime = self.stamp()

    def user_file(self):
        return os.path.join(self.user_dir, "devtime.toml")

    def project_files(self):
        return (sorted(glob.glob(os.path.join(self.user_dir, "projects", "*.toml"))) +
                sorted(glob.glob(os.path.join(self.root, "config", "projects", "*.toml"))))

    def stamp(self):
        files = [os.path.join(self.root, "config", "devtime.toml"), self.user_file()] + self.project_files()
        return max(os.path.getmtime(f) for f in files if os.path.exists(f))

    def path(self, key):
        """A path setting. cache_dir is always the current org's instance cache (paths.CACHE): a shared cache folder
        would mix organisations. Relative paths are under the data dir."""
        if key == "cache_dir":
            return paths.CACHE
        value = os.path.expanduser(self.settings.get(key, "") or "")
        if not value:
            return paths.DATA
        return value if os.path.isabs(value) else os.path.join(paths.DATA, value)

    def project_for(self, cwd):
        for project in self.projects:
            if project.owns(cwd):
                return project
        return None

    def classify(self, project, name, text):
        key = (project.name if project else None, name, text)
        kind = self._kind_cache.get(key)
        if kind is None:
            kind = self._classify(project, name, text)
            if len(self._kind_cache) > 500000:
                self._kind_cache.clear()
            self._kind_cache[key] = kind
        return kind

    def _classify(self, project, name, text):
        for rule in self.pre_rules + (project.rules if project else []) + self.rules:
            if rule.match(name, text):
                return rule.kind
        if SHELL_TOOL_RE.match(name):
            return "shell other"
        if name.startswith("mcp__"):
            return "mcp: " + name.split("__")[1]
        return self.tools.get(name, "tool: " + name)

    def price(self, model):
        for prefix, rates in self.pricing:
            if model.startswith(prefix):
                return rates
        return None
