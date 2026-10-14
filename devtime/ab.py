"""`python -m devtime ab ...`: A/B experiments on how Claude Code sessions work (models, agent types, instructions).

    ab new <id> --title T --arm name[:model[:agent_type]] (2+, repeatable) [--instructions name=text]
           [--opt <catalogue id>] [--dry-run]     write the experiment and a CLAUDE.md block telling sessions to ask
                                                  `ab arm` for each coding stage
    ab arm <id> <stage>                           the stage's arm, decided by hash (never by the session) - one line
    ab report <id> [--json]                       per arm: cost, minutes, output, failures, rework; caveats
    ab stop <id>                                  end it, remove the CLAUDE.md block, print the report
    ab list

Experiments live in <data dir>/config/experiments.toml ([[experiment]] id, title, active, started, opt, arms). The
CLAUDE.md block sits between <!-- devtime:ab:<id> --> and <!-- devtime:ab-end:<id> --> in <claude dir>/CLAUDE.md and runs
the stable launcher <data dir>/devtime.py (written at every session start). hooks/agent_budget.py refuses an agent
launch whose model / agent type differs from the arm of its tag.

Design (lessons from a hand-run A/B: the orchestrating session picked the arms, so the hard stages went to one arm;
fix-ups were named differently, so rework was missed; cost ignored how much each stage produced):
  - the arm is sha1(id:stage) mod number of arms: assigned by hash, not by judgement
  - a stage is every agent tagged `AB:<id>:<stage>`; fix-ups reuse the tag
  - a stage counts as reworked if a later agent (within 24 h) has the same tag, or is in the same work section with a
    fix / repair / rework / again / retry / follow-up / "...2" description, or has the stage's tag plus a trailing
    number (view-toggle -> view-toggle2)
  - outcomes: commits, edit calls, output tokens, tool errors, cost per commit - not only cost
  - fewer than MIN_STAGES stages in an arm: "too few stages to call"
"""

import difflib
import hashlib
import json
import os
import re
import statistics
import sys
import time
import tomllib

from . import paths, setup

MIN_STAGES = 10
REWORK_WINDOW_S = 24 * 3600
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TAG = re.compile(r"^AB:([\w-]+):([\w.-]+)", re.IGNORECASE)
FIX = re.compile(r"\b(?:fix(?:es|ed|ing)?|repair\w*|rework\w*|again|retry|retries|follow-?up)\b|2\s*$", re.IGNORECASE)


def experiments_path():
    return os.path.join(paths.USER_CONFIG, "experiments.toml")


# ---------------------------------------------------------------------------------------------------------------------
# Arms


def arm_index(exp_id, stage, n):
    """Which of n arms a stage belongs to: sha1(id + ":" + stage) mod n (lower case), the same on every machine."""
    digest = hashlib.sha1((str(exp_id).lower() + ":" + str(stage).lower()).encode("utf-8")).hexdigest()
    return int(digest, 16) % n


def arm_for(exp, stage):
    return exp["arms"][arm_index(exp["id"], stage, len(exp["arms"]))]


def same_model(wanted, actual):
    """True if the arm specifies no model, or `actual` (e.g. "opus", "claude-opus-4-5") names the wanted one."""
    if not wanted:
        return True
    wanted, actual = str(wanted).lower(), str(actual or "").lower()
    return bool(actual) and (wanted in actual or actual in wanted)


def same_agent_type(wanted, actual):
    if not wanted:
        return True
    wanted, actual = str(wanted).lower(), str(actual or "").lower()
    if wanted == actual:
        return True
    return (":" not in wanted or ":" not in actual) and wanted.split(":")[-1] == actual.split(":")[-1]


def arm_line(exp, stage):
    arm = arm_for(exp, stage)
    use = []
    if arm.get("model"):
        use.append('model "%s"' % arm["model"])
    if arm.get("agent_type"):
        use.append('subagent_type "%s"' % arm["agent_type"])
    text = "ARM %s of %s: launch the agent with %s" % (arm["name"], exp["id"], " and ".join(use) or "your usual settings")
    if arm.get("instructions"):
        text += "; add to its brief: %s" % arm["instructions"].strip().replace("\n", " ")
    return text + '; description must start "AB:%s:%s " (fix-ups of this stage reuse it).' % (exp["id"], stage)


# ---------------------------------------------------------------------------------------------------------------------
# experiments.toml


def load_experiments(path=None):
    try:
        with open(path or experiments_path(), "rb") as f:
            items = tomllib.load(f).get("experiment") or []
    except (OSError, ValueError):
        return []
    out = []
    for e in items:
        arms = [a for a in (e.get("arms") or []) if isinstance(a, dict) and a.get("name")]
        if e.get("id") and arms:
            out.append({"id": str(e["id"]).lower(), "title": str(e.get("title") or e["id"]),
                        "active": bool(e.get("active", True)), "started": str(e.get("started") or ""),
                        "opt": e.get("opt") or None, "arms": arms})
    return out


def find_experiment(exps, exp_id):
    for e in exps:
        if e["id"] == str(exp_id).lower():
            return e
    raise SystemExit("unknown experiment %r; known: %s" % (exp_id, ", ".join(e["id"] for e in exps) or "none"))


def q(text):
    return json.dumps(str(text), ensure_ascii=False)


def dump_experiments(exps):
    lines = ["# A/B experiments (python -m devtime ab ...); edit by hand only if you know what you are doing.", ""]
    for e in exps:
        lines += ["[[experiment]]", "id = %s" % q(e["id"]), "title = %s" % q(e["title"]),
                  "active = %s" % ("true" if e["active"] else "false"), "started = %s" % q(e["started"])]
        if e.get("opt"):
            lines.append("opt = %s" % q(e["opt"]))
        lines.append("arms = [")
        for a in e["arms"]:
            fields = ", ".join("%s = %s" % (k, q(a[k])) for k in ("name", "model", "agent_type", "instructions") if a.get(k))
            lines.append("  { %s }," % fields)
        lines += ["]", ""]
    return "\n".join(lines)


def save_experiments(exps, path=None):
    path = path or experiments_path()
    paths.ensure(os.path.dirname(path))
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_experiments(exps))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------------------------
# The CLAUDE.md block


def block_re(exp_id):
    start, end = "<!-- devtime:ab:%s -->" % exp_id, "<!-- devtime:ab-end:%s -->" % exp_id
    return re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)


def launcher_command():
    return 'python "%s"' % paths.launcher_path().replace("\\", "/")


def make_block(exp, eol="\n"):
    i = exp["id"]
    arms = ", ".join(a["name"] for a in exp["arms"])
    text = "\n".join([
        "<!-- devtime:ab:%s -->" % i,
        "## A/B test: %s (%s)" % (exp["title"], i),
        "- Arms: %s. For every coding stage of this kind, BEFORE launching its agent, run "
        "`%s ab arm %s <stage-slug>` (stage-slug: short, lower case, e.g. view-toggle) and follow its one-line output "
        "exactly: model, agent type, extra instructions." % (arms, launcher_command(), i),
        "- Name the agent `AB:%s:<stage-slug> <short description>`. Fix-ups, retries and follow-ups for the same stage "
        "reuse the same tag and arm; a new piece of work gets a new slug." % i,
        "- Do not choose or change arms yourself, even for a hard stage: a hook refuses launches that don't match "
        "the arm. The result is read with `ab report %s`." % i,
        "<!-- devtime:ab-end:%s -->" % i])
    return text.replace("\n", eol)


def diff_text(old, new):
    return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                        "a/CLAUDE.md" if old is not None else "/dev/null", "b/CLAUDE.md"))


def write_text(path, text):
    paths.ensure(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


# ---------------------------------------------------------------------------------------------------------------------
# new / stop / list / arm


def parse_arm(spec):
    parts = spec.split(":", 2)  # name[:model[:agent_type]]; the agent type may itself contain ':' (devtime:coder)
    arm = {"name": parts[0].strip()}
    if len(parts) > 1 and parts[1].strip():
        arm["model"] = parts[1].strip()
    if len(parts) > 2 and parts[2].strip():
        arm["agent_type"] = parts[2].strip()
    if not re.match(r"^[\w.-]+$", arm["name"]):
        raise SystemExit("arm name %r: use letters, digits, - . _" % arm["name"])
    return arm


def new_cmd(args):
    exps = load_experiments()
    exp_id = args.id.lower()
    if not ID_RE.match(exp_id):
        raise SystemExit("experiment id %r: use lower-case letters, digits, - and _" % args.id)
    if any(e["id"] == exp_id for e in exps):
        raise SystemExit("experiment %r exists; `ab stop %s` ends it, or pick another id" % (exp_id, exp_id))
    arms = [parse_arm(s) for s in args.arm or []]
    if len(arms) < 2 or len({a["name"] for a in arms}) != len(arms):
        raise SystemExit("need at least two arms with different names (--arm name[:model[:agent_type]])")
    for text in args.instructions or []:
        name, _, body = text.partition("=")
        arm = next((a for a in arms if a["name"] == name.strip()), None)
        if arm is None or not body.strip():
            raise SystemExit("--instructions %r: use armname=text with an existing arm name" % text)
        if len(body) > 400:
            raise SystemExit("--instructions for %s: keep it short (under 400 characters)" % name)
        arm["instructions"] = body.strip()
    title = args.title
    if args.opt:
        from . import optimize
        item = optimize.find_item(optimize.load_catalogue(), args.opt)
        title = title or item["title"]
    if not title:
        raise SystemExit("--title is required")
    exp = {"id": exp_id, "title": title, "active": True, "started": time.strftime("%Y-%m-%d"), "opt": args.opt or None,
           "arms": arms}
    md_path = setup.claude_md_path()
    old = setup.read_text(md_path)
    new, action = setup.upsert_block(old, block_re(exp_id), lambda eol: make_block(exp, eol))
    summary = {"id": exp_id, "claudeMd": md_path, "action": action,
               "arms": [dict(a, index=i) for i, a in enumerate(arms)], "launcher": launcher_command()}
    if args.dry_run:
        summary["dryRun"] = True
        print(json.dumps(summary, separators=(",", ":")))
        print(diff_text(old, new) or "(no change)")
        return 0
    paths.install_launcher()
    save_experiments(exps + [exp])
    if new != old:
        write_text(md_path, new)
    print(json.dumps(summary, separators=(",", ":")))
    return 0


def arm_cmd(args):
    exp = find_experiment(load_experiments(), args.id)
    if not exp["active"]:
        print("experiment %s is not active: no arm; launch the agent as you normally would." % exp["id"])
        return 1
    if not re.match(r"^[\w.-]+$", args.stage):
        raise SystemExit("stage slug %r: use letters, digits, - . _" % args.stage)
    print(arm_line(exp, args.stage.lower()))
    return 0


def list_cmd():
    exps = load_experiments()
    if not exps:
        print("no experiments; create one with `ab new` (see /devtime:ab)")
    for e in exps:
        print("%s [%s] %s since %s: %s%s" % (e["id"], "active" if e["active"] else "stopped", e["title"], e["started"],
                                            ", ".join(a["name"] + (("=" + a["model"]) if a.get("model") else "")
                                                      for a in e["arms"]), ("  (tests " + e["opt"] + ")") if e.get("opt") else ""))
    return 0


def stop_cmd(args):
    exps = load_experiments()
    exp = find_experiment(exps, args.id)
    exp["active"] = False
    save_experiments(exps)
    md_path = setup.claude_md_path()
    old = setup.read_text(md_path)
    new, removed = setup.remove_block(old, block_re(exp["id"])) if old else (old, False)
    if removed:
        write_text(md_path, new)
    print("stopped %s (CLAUDE.md block %s)" % (exp["id"], "removed" if removed else "was not there"))
    return report_cmd(args, exp)


# ---------------------------------------------------------------------------------------------------------------------
# Report


def agent_rows(data, idx, assign=None):
    """One row per subagent transcript: what the report needs. data / idx as in overview (no prompts, outputs or tool
    labels are read)."""
    from .overview import model_of, work_sections
    assign = work_sections() if assign is None else assign
    iv, tok, kinds = data["iv"], data["tok"], data["kinds"]
    edit_kind = kinds.index("edit code") if "edit code" in kinds else -1
    error = data["flags"].get("error", 2)
    edits, errors, out = {}, {}, {}
    for i, t in enumerate(iv["t"]):
        if iv["k"][i] == edit_kind:
            edits[t] = edits.get(t, 0) + 1
        if iv["f"][i] & error:
            errors[t] = errors.get(t, 0) + 1
    for j, i in enumerate(tok["i"]):
        t = iv["t"][i]
        out[t] = out.get(t, 0) + tok["out"][j]
    rows = []
    for t in data["transcripts"]:
        if t["role"] == "main":
            continue
        reqs = idx.reqs.get(t["idx"], [])
        if not reqs and not t.get("description"):
            continue
        section = assign.get(t["id"])
        rows.append({"id": t["id"], "start": t["start"], "end": t["end"], "desc": t.get("description") or "",
                     "type": t.get("agentType") or "", "model": model_of(t), "cost": sum(r[1] for r in reqs),
                     "commits": idx.commits_by_t.get(t["idx"], 0), "edits": edits.get(t["idx"], 0),
                     "errors": errors.get(t["idx"], 0), "out": out.get(t["idx"], 0),
                     "section": section if section and section != "-" else ""})
    return rows


def stem_of(stage):
    """Stage slug without a trailing number (view-toggle2 -> view-toggle); None if it has none."""
    m = re.match(r"^(.*?[^\d\-_.])[-_.]?\d+$", stage)
    return m.group(1) if m else None


def build_report(exp, rows):
    """Per-arm outcomes of the experiment's tagged stages. rows: agent_rows() (all subagents, so untagged fix-ups
    in the same work section are seen)."""
    exp_id = exp["id"]
    tagged = []
    for r in rows:
        m = TAG.match(r["desc"])
        if m and m.group(1).lower() == exp_id:
            tagged.append((m.group(2).lower(), r))
    slugs = {slug for slug, _ in tagged}
    stages, numbered = {}, set()
    for slug, r in tagged:  # view-toggle2 is a continuation of view-toggle (a fix-up named differently), not a stage
        stem = stem_of(slug)
        canon = stem if stem in slugs else slug
        if canon != slug:
            numbered.add(id(r))
        stages.setdefault(canon, []).append(r)
    n_arms = len(exp["arms"])
    out_stages = []
    for stage, agents in stages.items():
        agents.sort(key=lambda r: r["start"])
        first = agents[0]
        end = max(a["end"] for a in agents)
        arm = arm_for(exp, stage)
        sections = {a["section"] for a in agents if a["section"]}
        reasons = []
        for a in agents:
            if a is not first and first["end"] <= a["start"] <= end + REWORK_WINDOW_S:
                reasons.append("stem+digit" if id(a) in numbered else "same-tag")
        mine = {id(a) for a in agents}
        for r in rows:
            if id(r) in mine or not first["end"] <= r["start"] <= end + REWORK_WINDOW_S:
                continue
            if r["section"] and r["section"] in sections and FIX.search(r["desc"]):
                reasons.append("section-fix")
        out_stages.append({
            "stage": stage, "arm": arm["name"], "armIndex": arm_index(exp_id, stage, n_arms),
            "agents": len(agents), "cost": sum(a["cost"] for a in agents), "minutes": (end - first["start"]) / 60,
            "commits": sum(a["commits"] for a in agents), "edits": sum(a["edits"] for a in agents),
            "errors": sum(a["errors"] for a in agents), "out": sum(a["out"] for a in agents),
            "reworked": bool(reasons), "reasons": sorted(set(reasons)),
            "offArm": not (same_model(arm.get("model"), first["model"]) and
                           same_agent_type(arm.get("agent_type"), first["type"]))})
    arms = []
    for a in exp["arms"]:
        v = [s for s in out_stages if s["arm"] == a["name"]]
        row = {"arm": a["name"], "model": a.get("model"), "agentType": a.get("agent_type"), "stages": len(v)}
        if v:
            commits = sum(s["commits"] for s in v)
            reasons = {}
            for s in v:
                for why in s["reasons"]:
                    reasons[why] = reasons.get(why, 0) + 1
            row.update({
                "medianCost": statistics.median(s["cost"] for s in v), "totalCost": sum(s["cost"] for s in v),
                "medianMinutes": statistics.median(s["minutes"] for s in v),
                "commits": commits, "committedShare": sum(1 for s in v if s["commits"]) / len(v),
                "editsPerStage": sum(s["edits"] for s in v) / len(v),
                "medianOutTokens": statistics.median(s["out"] for s in v),
                "errorsPerStage": sum(s["errors"] for s in v) / len(v),
                "rework": sum(1 for s in v if s["reworked"]) / len(v), "reworkBy": reasons,
                "agentsPerStage": sum(s["agents"] for s in v) / len(v),
                "costPerCommit": sum(s["cost"] for s in v) / commits if commits else None,
                "offArm": sum(1 for s in v if s["offArm"])})
        arms.append(row)
    notes = []
    counts = [a["stages"] for a in arms]
    low = [a for a in arms if a["stages"] < MIN_STAGES]
    if low:
        notes.append("too few stages to call: %s (need >= %d per arm)" %
                     (", ".join("%s has %d" % (a["arm"], a["stages"]) for a in low), MIN_STAGES))
    if sum(counts) and (min(counts) == 0 or max(counts) / min(counts) > 1.5):
        notes.append("arms are unbalanced (%s)" % ", ".join("%s %d" % (a["arm"], a["stages"]) for a in arms))
    off = sum(a.get("offArm", 0) for a in arms)
    if off:
        notes.append("%d stage(s) ran on a different model / agent type than their arm (kept in their assigned arm)" % off)
    if not sum(counts):
        notes.append("no stages tagged AB:%s:<stage> yet" % exp_id)
    notes.append("rework is detected from tags, work sections and descriptions: undetected untagged fixes make an arm "
                 "look better than it is")
    return {"id": exp_id, "title": exp["title"], "active": exp["active"], "started": exp["started"], "opt": exp.get("opt"),
            "stages": len(out_stages), "arms": arms, "stageList": out_stages, "notes": notes, "minStages": MIN_STAGES}


def money(x):
    return "-" if x is None else "$%.2f" % x


def format_report(rep):
    lines = ["A/B %s (%s, since %s): %s - %d stages" % (rep["id"], "active" if rep["active"] else "stopped",
                                                         rep["started"], rep["title"], rep["stages"])]
    head = ("arm", "stages", "$/stage", "min/stage", "commits", "edits/st", "outk/st", "err/st", "rework", "$/commit")
    table = [head]
    for a in rep["arms"]:
        if not a["stages"]:
            table.append((a["arm"], "0") + ("-",) * 8)
            continue
        table.append((a["arm"], str(a["stages"]), money(a["medianCost"]), "%.0f" % a["medianMinutes"], str(a["commits"]),
                      "%.1f" % a["editsPerStage"], "%.1f" % (a["medianOutTokens"] / 1000), "%.1f" % a["errorsPerStage"],
                      "%.0f%%" % (100 * a["rework"]), money(a["costPerCommit"])))
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    for r in table:
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    for a in rep["arms"]:
        if a.get("reworkBy"):
            lines.append("  rework %s: %s" % (a["arm"], ", ".join("%s %d" % kv for kv in sorted(a["reworkBy"].items()))))
    lines += ["  note: " + n for n in rep["notes"]]
    return "\n".join(lines)


def gather(now=None):
    """agent_rows of every subagent in the dataset (scans transcripts)."""
    from .overview import Index
    from .store import Store
    store = Store(verbose=False)
    for attempt in range(5):
        try:
            store.refresh()
            break
        except PermissionError:  # the server saving its cache at the same moment
            time.sleep(1)
    else:
        store.refresh()
    data = store.slice("*")
    return agent_rows(data, Index(data))


def report_cmd(args, exp=None):
    exp = exp or find_experiment(load_experiments(), args.id)
    rep = build_report(exp, gather())
    print(json.dumps(rep, separators=(",", ":")) if getattr(args, "json", False) else format_report(rep))
    return 0


def dashboard_rows(rows):
    """Rows for the dashboard's experiments table (overview.experiments): one per experiment arm with stages."""
    out = []
    for exp in load_experiments():
        for a in build_report(exp, rows)["arms"]:
            if a["stages"]:
                out.append({"experiment": exp["id"], "model": a["arm"], "stages": a["stages"],
                            "medianCost": a["medianCost"], "totalCost": a["totalCost"],
                            "medianMinutes": a["medianMinutes"], "rework": a["rework"],
                            "agentsPerStage": a["agentsPerStage"], "costPerCommit": a["costPerCommit"]})
    return out


def main(args):
    cmd = args.ab_cmd
    if cmd == "new":
        return new_cmd(args)
    if cmd == "arm":
        return arm_cmd(args)
    if cmd == "report":
        return report_cmd(args)
    if cmd == "stop":
        return stop_cmd(args)
    return list_cmd()
