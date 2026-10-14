"use strict";
// Inside Unreal runs: editor logs split into phases / activities / script steps, matched to the agent calls that
// launched them (data.ue, built by devtime/unreal.py + analyze.py). Uses the globals of app.js.

const UE_PHASES = ["startup", "map load", "editor idle", "script", "shutdown", "outside the editor"];
const UE_PHASE_SLOT = { startup: 2, "map load": 4, "editor idle": 5, script: 1, shutdown: 7 };
const UE_POLL = "polling / waiting on a logged editor run";
const UE_LOST = "editor launched, log lost (purged or overwritten)";
const UE_IDLEWAIT = "waiting / polling, no logged run going";
const UE_NONE = "no editor involved (description only)";
// An editor in the background is throttled to 3 fps unless it runs -unattended; a rendering run this slow was.
const THROTTLED_FPS = 4.0;
const ueThrottled = (R, r) => R.fps && R.fps[r] != null && R.fps[r] < THROTTLED_FPS && !/-nullrhi|-unattended/.test(R.flags[r] || "");
const ueFps = (R, r) => (R.fps && R.fps[r] != null ? `${R.fps[r] >= 10 ? Math.round(R.fps[r]) : R.fps[r].toFixed(1)} fps` : "");
const uePhaseColor = (p) => (UE_PHASE_SLOT[p] ? palette[UE_PHASE_SLOT[p]] : palette["--other"]);
let ueSort = "slow";

function prepUE(data) {
  const ue = data.ue;
  if (!ue || !ue.runs || !ue.runs.s.length) return null;
  const R = ue.runs, nR = R.s.length;
  const phByRun = Array.from({ length: nR }, () => []);
  ue.ph.r.forEach((r, j) => phByRun[r].push(j));
  const stByRun = Array.from({ length: nR }, () => []);
  ue.st.r.forEach((r, j) => stByRun[r].push(j));
  const runsByIv = new Map();
  for (let r = 0; r < nR; r++) {
    const i = R.iv[r];
    if (i < 0) continue;
    if (!runsByIv.has(i)) runsByIv.set(i, []);
    runsByIv.get(i).push(r);
  }
  // Per launching call: the fraction of its duration in each editor phase; the rest happened outside the editor.
  const iv = data.iv, fracs = new Map();
  for (const [i, rs] of runsByIv) {
    const s = iv.s[i], e = iv.e[i], dur = Math.max(e - s, 1e-6);
    const per = new Map();
    let covered = 0;
    for (const r of rs) for (const j of phByRun[r]) {
      const a = Math.max(ue.ph.s[j], s), b = Math.min(ue.ph.e[j], e);
      if (b <= a) continue;
      const p = ue.phases[ue.ph.p[j]];
      per.set(p, (per.get(p) || 0) + (b - a));
      covered += b - a;
    }
    const scale = covered > dur ? dur / covered : 1;
    const out = [...per].map(([p, v]) => [p, v * scale / dur]);
    const rest = 1 - Math.min(1, covered * scale / dur);
    if (rest > 1e-4) out.push(["outside the editor", rest]);
    fracs.set(i, out);
  }
  // Calls of the "unreal run" kind that launched no logged run: polling / waiting on a run that was going, or a run
  // whose log is gone (Unreal purges old logs; harvesting only started with this dashboard).
  const spans = [];
  for (let r = 0; r < nR; r++) spans.push([R.s[r], R.e[r]]);
  spans.sort((a, b) => a[0] - b[0]);
  const ki = data.kinds.indexOf("unreal run");
  const FL = data.flags, scripts = data.launchScripts || {};
  const orphan = new Map(), lost = new Map();
  for (let i = 0; i < iv.t.length; i++) {
    const launcher = iv.f[i] & FL.launch;
    if (runsByIv.has(i) || iv.k[i] !== ki || iv.f[i] & FL.noshare) continue;
    let ov = 0;
    for (const [a, b] of spans) { if (a > iv.e[i]) break; const o = Math.min(b, iv.e[i]) - Math.max(a, iv.s[i]); if (o > 0) ov += o; }
    const dur = Math.max(iv.e[i] - iv.s[i], 1e-6);
    const f = Math.min(1, ov / dur);
    const parts = [];
    if (f > 0) parts.push([UE_POLL, f]);
    if (f < 1) {
      const why = launcher ? UE_LOST : iv.f[i] & FL.wait ? UE_IDLEWAIT : UE_NONE;
      parts.push([why, 1 - f]);
      if (launcher) lost.set(i, { script: scripts[i] || "(no script)", frac: 1 - f });
    }
    if (iv.k[i] === ki) orphan.set(i, parts);
  }
  return { ue, nR, phByRun, stByRun, runsByIv, fracs, orphan, lost };
}

// Per kind: seconds of matched calls by editor phase (for the "split Unreal runs" rows).
function ueSplitByKind() {
  const U = S.ue;
  const out = new Map();
  if (!U) return out;
  const iv = S.data.iv;
  for (const [i, fr] of [...U.fracs, ...U.orphan]) {
    const w = F.weight(i);
    if (w <= 0 || iv.f[i] & S.data.flags.noshare) continue;
    const kind = S.data.kinds[iv.k[i]];
    let m = out.get(kind);
    if (!m) out.set(kind, (m = new Map()));
    for (const [p, f] of fr) m.set(p, (m.get(p) || 0) + w * f);
  }
  return out;
}

function ueRunPass(r) {
  const U = S.ue, R = U.ue.runs;
  if (R.e[r] < F.lo || R.s[r] > F.hi) return false;
  const i = R.iv[r];
  if (i >= 0) return F.pass(i);
  const f = S.f;
  return $("#opt-ue-unmatched").checked && !f.session && !f.role && !f.ws && !f.branch && !f.search;
}

function renderUnreal() {
  const card = $("#ue-card");
  const U = S.ue;
  card.hidden = !U;
  if (!U) return;
  const ue = U.ue, R = ue.runs;
  const runs = [];
  for (let r = 0; r < U.nR; r++) if (ueRunPass(r)) runs.push(r);
  // Phase and activity totals (wall-clock inside the logs) + outside-the-editor time of the launching calls.
  const byPhase = new Map(), byAct = new Map();
  let editor = 0;
  for (const r of runs) {
    for (const j of U.phByRun[r]) {
      const d = ue.ph.e[j] - ue.ph.s[j];
      const p = ue.phases[ue.ph.p[j]], a = ue.activities[ue.ph.a[j]];
      byPhase.set(p, (byPhase.get(p) || 0) + d);
      if (!byAct.has(p)) byAct.set(p, new Map());
      byAct.get(p).set(a, (byAct.get(p).get(a) || 0) + d);
      editor += d;
    }
  }
  let outside = 0, callTime = 0;
  const iv = S.data.iv;
  const calls = new Set(runs.map((r) => R.iv[r]).filter((i) => i >= 0));
  for (const i of calls) {
    const dur = iv.e[i] - iv.s[i];
    callTime += dur;
    const fr = U.fracs.get(i) || [];
    const rest = fr.find(([p]) => p === "outside the editor");
    if (rest) outside += rest[1] * dur;
  }
  if (outside > 0) byPhase.set("outside the editor", outside);
  // Launching calls whose run left no log: before harvesting began (Unreal purges old logs) or a log name reused by
  // the next run. Their time is known, their inside is not.
  const lostByScript = new Map();
  let lostTime = 0, lostN = 0;
  for (const [i, o] of U.lost) {
    if (!F.pass(i)) continue;
    const t = F.weight(i) > 0 ? (iv.e[i] - iv.s[i]) * o.frac : 0;
    if (t <= 0) continue;
    lostTime += t; lostN++;
    const m = lostByScript.get(o.script) || { n: 0, t: 0 };
    m.n++; m.t += t;
    lostByScript.set(o.script, m);
  }
  const matched = runs.filter((r) => R.iv[r] >= 0).length;
  const ki = S.prep.kindIndex.get("unreal run");
  let urun = 0;
  if (ki != null) for (let i = 0; i < S.prep.n; i++) if (iv.k[i] === ki && F.pass(i)) urun += iv.e[i] - iv.s[i];
  let urunMatched = 0;
  for (const i of calls) if (iv.k[i] === ki) urunMatched += iv.e[i] - iv.s[i];
  const throttled = runs.filter((r) => ueThrottled(R, r));
  const throttledTime = throttled.reduce((a, r) => a + R.e[r] - R.s[r], 0);
  const crashed = runs.filter((r) => ue.exits[R.exit[r]] === "crash");
  const crashTime = crashed.reduce((a, r) => a + R.e[r] - R.s[r], 0);
  $("#ue-sub").textContent = `${runs.length} editor runs (${matched} launched by agent calls) · ${fmtHours(editor)} in the editor`
    + (throttled.length ? ` · ${throttled.length} throttled to ~3 fps (${fmtDur(throttledTime)}; launched without -unattended)` : "")
    + (crashed.length ? ` · ${crashed.length} crashed (${fmtDur(crashTime)} lost)` : "")
    + (urun ? ` · runs with logs account for ${fmtPct(Math.min(1, urunMatched / urun))} of "unreal run" call time (the rest: polling running editors, runs whose logs are gone)` : "");

  // Phases with their activities.
  const total = [...byPhase.values()].reduce((a, b) => a + b, 0) || 1;
  const box = el("div", { class: "bars" });
  const maxP = Math.max(1, ...byPhase.values());
  for (const p of UE_PHASES.filter((p) => byPhase.has(p)).sort((a, b) => byPhase.get(b) - byPhase.get(a))) {
    const v = byPhase.get(p), c = uePhaseColor(p);
    addBarRow(box, p, v, total, maxP, c, false, p === "outside the editor"
      ? ["time the launching calls spent outside any editor log: GPU-lock waits inside scripts, builds, process launch, waits after exit"] : []);
    const acts = [...(byAct.get(p) || [])].sort((a, b) => b[1] - a[1]);
    const shown = acts.slice(0, 6);
    if (acts.length > 6) shown.push([`${acts.length - 6} more`, acts.slice(6).reduce((a, x) => a + x[1], 0)]);
    for (const [a, av] of shown) if (av / total > 0.002) addBarRow(box, a, av, total, maxP, c, true);
  }
  if (lostTime > 0) {
    addBarRow(box, `log lost (${lostN} launches)`, lostTime, total + lostTime, Math.max(maxP, lostTime), palette["--idle"], false,
      ["editor runs whose log is gone: launched before this dashboard harvested logs (Unreal purges old ones),",
        "or a log name the next run reused. From now on logs are archived as they are written."]);
  }
  $("#ue-phases").replaceChildren(runs.length || lostTime ? box : el("div", { class: "sub" }, "No Unreal runs in this filter."));
  $("#ue-phase-legend").replaceChildren(...UE_PHASES.filter((p) => byPhase.has(p)).map((p) => el("span", {}, el("i", { style: `background:${uePhaseColor(p)}` }), p)));

  // Script steps by kind.
  const byKind = new Map(), stepAgg = new Map();
  for (const r of runs) {
    const script = ue.scripts[R.script[r]];
    for (const j of U.stByRun[r]) {
      const d = ue.st.e[j] - ue.st.s[j];
      const k = ue.stepKinds[ue.st.k[j]];
      byKind.set(k, (byKind.get(k) || 0) + d);
      const key = script + "\u0000" + ue.st.l[j];
      let o = stepAgg.get(key);
      if (!o) stepAgg.set(key, (o = { script, label: ue.labels[ue.st.l[j]], kind: k, n: 0, total: 0, max: 0, maxR: r }));
      o.n++; o.total += d;
      if (d > o.max) { o.max = d; o.maxR = r; }
    }
  }
  const kbox = el("div", { class: "bars" });
  const ktotal = [...byKind.values()].reduce((a, b) => a + b, 0) || 1, kmax = Math.max(1, ...byKind.values());
  for (const [k, v] of [...byKind].sort((a, b) => b[1] - a[1])) addBarRow(kbox, k, v, ktotal, kmax, uePhaseColor("script"), false);
  $("#ue-stepkinds").replaceChildren(byKind.size ? kbox : el("div", { class: "sub" }, "No script steps logged."));

  renderFindings(runs);
  renderTests(runs);
  renderAnatomy(runs);

  // By script.
  const scripts = new Map();
  for (const r of runs) {
    const name = ue.scripts[R.script[r]];
    let o = scripts.get(name);
    if (!o) scripts.set(name, (o = { name, n: 0, total: 0, startup: 0, map: 0, script: 0, shutdown: 0, crash: 0, unknown: 0, shots: 0, last: 0, fps: [], throttled: 0, throttledT: 0 }));
    o.n++; o.total += R.e[r] - R.s[r]; o.last = Math.max(o.last, R.s[r]);
    if (R.fps && R.fps[r] != null && !/-nullrhi/.test(R.flags[r] || "")) o.fps.push(R.fps[r]);
    if (ueThrottled(R, r)) { o.throttled++; o.throttledT += R.e[r] - R.s[r]; }
    const ex = ue.exits[R.exit[r]];
    if (ex === "crash") o.crash++; else if (ex === "unknown") o.unknown++;
    for (const j of U.phByRun[r]) {
      const d = ue.ph.e[j] - ue.ph.s[j], p = ue.phases[ue.ph.p[j]];
      if (p === "startup") o.startup += d; else if (p === "map load") o.map += d; else if (p === "script" || p === "editor idle") o.script += d; else if (p === "shutdown") o.shutdown += d;
    }
    for (const j of U.stByRun[r]) if (/screenshot|capture/.test(ue.stepKinds[ue.st.k[j]])) o.shots += ue.st.e[j] - ue.st.s[j];
  }
  for (const [name, m] of lostByScript) {
    let o = scripts.get(name);
    if (!o) scripts.set(name, (o = { name, n: 0, total: 0, startup: 0, map: 0, script: 0, shutdown: 0, crash: 0, unknown: 0, shots: 0, last: 0 }));
    o.lostN = m.n; o.lost = m.t;
  }
  const avg = (o, k) => (o.n ? fmtDur(o[k] / o.n) : "");
  table($("#ue-scripts"), [
    { key: "name", label: "Script", cls: "label" },
    { key: "n", label: "Runs", num: true },
    { key: "total", label: "Total", num: true, render: (o) => fmtDur(o.total) },
    { key: "avg", label: "Avg run", num: true, sortv: (o) => o.total / (o.n || 1), render: (o) => avg(o, "total") },
    { key: "startup", label: "Avg startup", num: true, sortv: (o) => o.startup / o.n, render: (o) => avg(o, "startup") },
    { key: "map", label: "Avg map load", num: true, sortv: (o) => o.map / o.n, render: (o) => avg(o, "map") },
    { key: "script", label: "Avg script", num: true, sortv: (o) => o.script / o.n, render: (o) => avg(o, "script") },
    { key: "shots", label: "Screenshots", num: true, render: (o) => fmtDur(o.shots) },
    { key: "shutdown", label: "Avg shutdown", num: true, sortv: (o) => o.shutdown / o.n, render: (o) => avg(o, "shutdown") },
    { key: "fps", label: "Median fps", num: true, title: () => "engine frames per second once started, rendering runs only", sortv: (o) => median(o.fps || []), render: (o) => (o.fps && o.fps.length ? fmtFps(median(o.fps)) : "") },
    { key: "throttled", label: "Throttled", num: true, title: (o) => (o.throttled ? `${o.throttled} runs at ~3 fps: launched without -unattended` : ""), sortv: (o) => o.throttledT || 0, render: (o) => (o.throttled ? `${o.throttled} (${fmtDur(o.throttledT)})` : "") },
    { key: "crash", label: "Crashed", num: true, render: (o) => (o.crash ? String(o.crash) : "") },
    { key: "lost", label: "Lost-log time", num: true, title: (o) => (o.lostN ? `${o.lostN} launches whose log is gone` : ""), sortv: (o) => o.lost || 0, render: (o) => (o.lost ? `${fmtDur(o.lost)} (${o.lostN})` : "") },
    { key: "last", label: "Last run", num: true, render: (o) => (o.last ? fmtWhen(o.last) : "") },
  ], [...scripts.values()], { sort: "total" });

  // Slowest steps.
  table($("#ue-steps"), [
    { key: "total", label: "Total", num: true, render: (o) => fmtDur(o.total) },
    { key: "n", label: "Count", num: true },
    { key: "avg", label: "Avg", num: true, sortv: (o) => o.total / o.n, render: (o) => fmtDur(o.total / o.n) },
    { key: "max", label: "Max", num: true, render: (o) => fmtDur(o.max) },
    { key: "kind", label: "Kind" },
    { key: "label", label: "Step (what the script logged when it finished)", cls: "label", title: (o) => o.label },
    { key: "script", label: "Script", cls: "label" },
  ], [...stepAgg.values()].sort((a, b) => b.total - a.total).slice(0, 300), {
    sort: "total", onclick: (o) => { const i = R.iv[o.maxR]; if (i >= 0) focusInterval(i); },
  });
}

function median(values) {
  if (!values.length) return 0;
  const v = [...values].sort((a, b) => a - b), m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}
const fmtFps = (x) => (x >= 10 ? String(Math.round(x)) : x.toFixed(1));

function addBarRow(box, name, v, total, max, color, sub, extraTip = []) {
  const row = [el("div", { class: "name" + (sub ? " sub" : "") }, sub ? null : el("i", { style: `background:${color}` }), name),
    el("div", { class: "track" }, el("b", { style: `width:${(100 * v / max).toFixed(2)}%;background:${color};opacity:${sub ? 0.55 : 1}` })),
    el("div", { class: "num" + (sub ? " sub" : "") }, fmtDur(v)), el("div", { class: "pct" + (sub ? " sub" : "") }, fmtPct(v / total))];
  for (const n of row) {
    n.addEventListener("mousemove", (ev) => showTip(ev, [[name, color], fmtDur(v), `${fmtPct(v / total)}`, ...extraTip]));
    n.addEventListener("mouseleave", hideTip);
  }
  box.append(...row);
}

// Issues the harvester flagged per run (devtime/unreal.py detect_issues), grouped by pattern, script and label.
function renderFindings(runs) {
  const U = S.ue, ue = U.ue, R = ue.runs, I = ue.issues;
  if (!I) return;
  const pass = new Set(runs);
  const groups = new Map();
  for (let j = 0; j < I.r.length; j++) {
    const r = I.r[j];
    if (!pass.has(r)) continue;
    const kind = ue.issueKinds[I.k[j]];
    const label = kind === "throttled" ? "" : ue.labels[I.l[j]];
    const script = ue.scripts[R.script[r]];
    const key = kind + "\u0000" + script + "\u0000" + label;
    let g = groups.get(key);
    if (!g) groups.set(key, (g = { kind, title: (ue.issueTitles || {})[kind] || kind, advice: (ue.issueAdvice || {})[kind] || "",
      script, label, secs: 0, n: 0, runs: new Set(), last: 0, lastR: r }));
    g.secs += I.s[j]; g.n++; g.runs.add(r);
    if (R.e[r] > g.last) { g.last = R.e[r]; g.lastR = r; }
  }
  const rows = [...groups.values()].map((g) => ({ ...g, runs: g.runs.size }));
  table($("#ue-findings"), [
    { key: "secs", label: "Time", num: true, render: (g) => fmtDur(g.secs) },
    { key: "title", label: "Pattern", title: (g) => `${g.title}. Fix: ${g.advice}` },
    { key: "script", label: "Script", cls: "label" },
    { key: "label", label: "Where (step / detail)", cls: "label", title: (g) => g.label },
    { key: "n", label: "Count", num: true },
    { key: "runs", label: "Runs", num: true },
    { key: "last", label: "Last seen", num: true, render: (g) => fmtWhen(g.last) },
  ], rows, { sort: "secs", limit: 60, onclick: (g) => { const i = R.iv[g.lastR]; if (i >= 0) focusInterval(i); } });
}

// Automation tests across the runs in the filter: per test path, how often, how long, how often it failed.
function renderTests(runs) {
  const U = S.ue, ue = U.ue, R = ue.runs, T = ue.tests;
  const host = $("#ue-tests");
  if (!T || !T.r.length) { host.replaceChildren(el("div", { class: "sub" }, "No automation tests in these logs.")); return; }
  const pass = new Set(runs);
  const by = new Map();
  for (let j = 0; j < T.r.length; j++) {
    const r = T.r[j];
    if (!pass.has(r)) continue;
    const path = ue.testPaths[T.p[j]], d = T.e[j] - T.s[j], res = ue.testResults[T.res[j]];
    let o = by.get(path);
    if (!o) by.set(path, (o = { path, n: 0, total: 0, ds: [], fails: 0, last: 0, lastR: r, runs: new Set() }));
    o.n++; o.total += d; o.ds.push(d); o.runs.add(r);
    if (res !== "Success") o.fails++;
    if (T.e[j] > o.last) { o.last = T.e[j]; o.lastR = r; }
  }
  const rows = [...by.values()].map((o) => ({ ...o, median: median(o.ds), max: Math.max(...o.ds), runs: o.runs.size }));
  const total = rows.reduce((a, o) => a + o.total, 0);
  table(host, [
    { key: "path", label: "Test", cls: "label", title: (o) => o.path },
    { key: "total", label: "Total", num: true, render: (o) => fmtDur(o.total) },
    { key: "share", label: "Share", num: true, sortv: (o) => o.total, render: (o) => fmtPct(o.total / (total || 1)) },
    { key: "n", label: "Runs", num: true },
    { key: "median", label: "Median", num: true, render: (o) => fmtDur(o.median) },
    { key: "max", label: "Max", num: true, render: (o) => fmtDur(o.max) },
    { key: "fails", label: "Failed", num: true, render: (o) => (o.fails ? `${o.fails} (${fmtPct(o.fails / o.n)})` : "") },
    { key: "last", label: "Last run", num: true, render: (o) => fmtWhen(o.last) },
  ], rows, { sort: "total", limit: 200, onclick: (o) => { const i = R.iv[o.lastR]; if (i >= 0) focusInterval(i); } });
}

// One row per run: its phases in sequence from the run's start, so startup / map load / script / shutdown compare.
function renderAnatomy(runs) {
  const U = S.ue, ue = U.ue, R = ue.runs;
  const list = [...runs].sort(ueSort === "slow" ? (a, b) => (R.e[b] - R.s[b]) - (R.e[a] - R.s[a]) : (a, b) => R.s[b] - R.s[a]).slice(0, 40);
  const host = $("#ue-anatomy");
  if (!list.length) { host.replaceChildren(); return; }
  const W = Math.max(400, host.clientWidth || 900), LW = 300, RH = 18, H = list.length * RH + 24;
  const maxD = Math.max(...list.map((r) => R.e[r] - R.s[r]));
  const x = (t) => LW + t / maxD * (W - LW - 60);
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Phases of each Unreal run" });
  // axis
  const step = [10, 30, 60, 120, 300, 600, 900, 1800, 3600].find((s) => maxD / s <= 8) || 3600;
  for (let t = 0; t <= maxD; t += step) {
    root.append(svg("line", { x1: x(t), x2: x(t), y1: 14, y2: H, stroke: palette["--grid"] }));
    const tx = svg("text", { x: x(t) + 2, y: 10, fill: palette["--text-muted"], "font-size": 11 }); tx.textContent = fmtDur(t); root.append(tx);
  }
  list.forEach((r, row) => {
    const y = 18 + row * RH;
    const label = svg("text", { x: 0, y: y + 11, fill: palette["--text-primary"], "font-size": 12 });
    const t = R.t[r] >= 0 ? S.data.transcripts[R.t[r]] : null;
    label.textContent = `${ue.scripts[R.script[r]]} · ${fmtWhen(R.s[r])}` + (ueThrottled(R, r) ? ` · ${ueFps(R, r)} (throttled)` : "");
    const clip = svg("title"); clip.textContent = `${ue.logs[R.log[r]]} (${ue.checkouts[R.checkout[r]]})${t ? " · " + (t.description || t.title) : ""}`;
    label.append(clip);
    root.append(label);
    // merge consecutive phases
    const segs = [];
    for (const j of U.phByRun[r]) {
      const p = ue.phases[ue.ph.p[j]];
      const last = segs[segs.length - 1];
      if (last && last.p === p) { last.e = ue.ph.e[j]; last.acts.set(ue.activities[ue.ph.a[j]], (last.acts.get(ue.activities[ue.ph.a[j]]) || 0) + ue.ph.e[j] - ue.ph.s[j]); }
      else segs.push({ p, s: ue.ph.s[j], e: ue.ph.e[j], acts: new Map([[ue.activities[ue.ph.a[j]], ue.ph.e[j] - ue.ph.s[j]]]) });
    }
    for (const sg of segs) {
      const x0 = x(sg.s - R.s[r]), x1 = x(sg.e - R.s[r]);
      const rect = svg("rect", { x: x0, y: y + 2, width: Math.max(0.8, x1 - x0 - (x1 - x0 > 3 ? 1 : 0)), height: RH - 6, fill: uePhaseColor(sg.p), rx: 1 });
      rect.addEventListener("mousemove", (ev) => showTip(ev, [{ k: uePhaseColor(sg.p), v: fmtDur(sg.e - sg.s), m: sg.p },
        ...[...sg.acts].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([a, v]) => ({ v: fmtDur(v), m: a })),
        { m: `${ue.scripts[R.script[r]]} · ${ue.logs[R.log[r]]} · exit ${ue.exits[R.exit[r]]}` },
        { m: [ueFps(R, r), R.flags[r] || "", ueThrottled(R, r) ? "throttled: launched without -unattended" : ""].filter(Boolean).join(" · ") }]));
      rect.addEventListener("mouseleave", hideTip);
      root.append(rect);
    }
    const tot = svg("text", { x: x(R.e[r] - R.s[r]) + 6, y: y + 11, fill: palette["--text-secondary"], "font-size": 11 });
    tot.textContent = fmtDur(R.e[r] - R.s[r]) + (ue.exits[R.exit[r]] === "crash" ? " crash" : "");
    root.append(tot);
    const hit = svg("rect", { x: 0, y, width: LW - 8, height: RH, fill: "transparent", style: "cursor:pointer" });
    hit.addEventListener("click", () => { if (R.iv[r] >= 0) focusInterval(R.iv[r]); });
    root.append(hit);
  });
  host.replaceChildren(root);
}

// Extra tooltip lines for a call that launched Unreal runs.
function ueTipLines(i) {
  const U = S.ue;
  if (!U || !U.runsByIv.has(i)) return [];
  const ue = U.ue, R = ue.runs, lines = [];
  for (const r of U.runsByIv.get(i).slice(0, 4)) {
    const per = new Map();
    for (const j of U.phByRun[r]) { const p = ue.phases[ue.ph.p[j]]; per.set(p, (per.get(p) || 0) + ue.ph.e[j] - ue.ph.s[j]); }
    lines.push({ m: `Unreal ${ue.scripts[R.script[r]]}${R.fps && R.fps[r] != null ? ` (${ueFps(R, r)}${ueThrottled(R, r) ? ", throttled" : ""})` : ""}: ` + UE_PHASES.filter((p) => per.has(p)).map((p) => `${p} ${fmtDur(per.get(p))}`).join(" · ") });
  }
  const fr = U.fracs.get(i) || [];
  const rest = fr.find(([p]) => p === "outside the editor");
  if (rest) lines.push({ m: `outside the editor ${fmtDur(rest[1] * (S.data.iv.e[i] - S.data.iv.s[i]))}` });
  return lines;
}

function setupUnreal() {
  $("#opt-ue-unmatched").addEventListener("change", () => renderUnreal());
  $("#opt-ue-split").addEventListener("change", () => renderKinds());
  $("#opt-ue-sort").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    ueSort = b.dataset.v;
    for (const x of $("#opt-ue-sort").children) x.classList.toggle("on", x === b);
    renderUnreal();
  });
}
