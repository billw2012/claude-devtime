"use strict";
// Dev time dashboard: everything is computed client-side from /api/data (columnar intervals), so every filter
// re-slices the same numbers and all panels agree.

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return n;
};
const SVGNS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

// ---------------------------------------------------------------------------------------------------------- format
function fmtDur(s) {
  s = Math.max(0, s || 0);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m === 60 ? `${h + 1}h00` : `${h}h${String(m).padStart(2, "0")}`;
}
const fmtHours = (s) => (s / 3600 >= 100 ? (s / 3600).toFixed(0) : (s / 3600).toFixed(1)) + " h";
const fmtPct = (x) => (x >= 0.1 ? (100 * x).toFixed(0) : (100 * x).toFixed(1)) + " %";
const fmtMoney = (x) => "$" + (x >= 100 ? Math.round(x).toLocaleString() : x.toFixed(2));
function fmtNum(x) {
  if (x >= 1e9) return (x / 1e9).toFixed(1) + "B";
  if (x >= 1e6) return (x / 1e6).toFixed(1) + "M";
  if (x >= 1e4) return (x / 1e3).toFixed(0) + "K";
  if (x >= 1e3) return (x / 1e3).toFixed(1) + "K";
  return String(Math.round(x));
}
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad2 = (n) => String(n).padStart(2, "0");
const fmtTime = (t) => { const d = new Date(t * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const fmtDay = (t) => { const d = new Date(t * 1000); return `${DOW[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`; };
const fmtWhen = (t) => `${fmtDay(t)} ${fmtTime(t)}`;

// ---------------------------------------------------------------------------------------------------------- state
const S = {
  project: null, data: null, version: -1, prep: null,
  f: { range: "all", from: null, to: null, session: "", role: "", ws: "", branch: "", search: "" },
  includeWaiting: false, bucket: "auto", slowKind: "",
  colors: {}, // project -> {kind: slot}
  tl: { v0: 0, v1: 1, rows: [], waiting: true, model: true, highlight: null },
};
const WAIT_COLORS = {
  "waiting on user": "--wait-user", "editor open for user": "--wait-user", "waiting on subagent": "--wait-sub",
  "waiting on wakeup": "--wait-sub", idle: "--idle", overhead: "--idle",
};

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
let palette = {};
function readPalette() {
  palette = {};
  for (let i = 1; i <= 8; i++) palette[i] = cssVar(`--s${i}`);
  for (const v of ["--other", "--wait-user", "--wait-sub", "--idle", "--grid", "--axis", "--text-muted",
    "--text-secondary", "--text-primary", "--surface-1", "--surface-2", "--accent"]) palette[v] = cssVar(v);
}

// Colour follows the kind: model is always slot 1, a GPU-lock wait is slot 8 (red), the rest of the busiest kinds
// take the remaining slots in rank order, once per project, and keep them across live refreshes.
function assignColors() {
  const P = S.prep, key = S.project;
  const map = S.colors[key] || (S.colors[key] = {});
  const used = new Set(Object.values(map));
  const ranked = P.kindTotals.filter(([k]) => !P.waitKind.has(k) && !(k in map));
  const take = (kind, slot) => { if (!used.has(slot) && !(kind in map)) { map[kind] = slot; used.add(slot); } };
  if (P.kindIndex.has("model")) take("model", 1);
  if (P.kindIndex.has("gpu lock wait")) take("gpu lock wait", 8);
  for (const [k] of ranked) {
    if (k in map) continue;
    for (let s = 2; s <= 8; s++) if (!used.has(s)) { take(k, s); break; }
  }
}
function colorOf(kind) {
  if (WAIT_COLORS[kind]) return palette[WAIT_COLORS[kind]];
  const slot = (S.colors[S.project] || {})[kind];
  return slot ? palette[slot] : palette["--other"];
}
const isOther = (kind) => !WAIT_COLORS[kind] && !(S.colors[S.project] || {})[kind];

// ---------------------------------------------------------------------------------------------------------- data
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function prepare(data) {
  const iv = data.iv, n = iv.t.length;
  const waitKind = new Set([...data.waiting, "overhead"]);
  const kindIsWait = data.kinds.map((k) => waitKind.has(k));
  const hours = new Map();
  for (const [i, h, s] of data.hours) {
    let a = hours.get(i);
    if (!a) hours.set(i, (a = []));
    a.push(h, s);
  }
  const tok = new Map();
  data.tok.i.forEach((i, j) => tok.set(i, j));
  const root = data.transcripts.map((t) => (t.parent != null ? t.parent : t.idx));
  const NOSHARE = data.flags.noshare;
  const totals = new Map();
  let t0 = Infinity, t1 = -Infinity;
  for (let i = 0; i < n; i++) {
    if (!(iv.f[i] & NOSHARE)) {
      const k = data.kinds[iv.k[i]];
      totals.set(k, (totals.get(k) || 0) + iv.w[i]);
    }
    if (iv.s[i] < t0) t0 = iv.s[i];
    if (iv.e[i] > t1) t1 = iv.e[i];
  }
  const byT = data.transcripts.map(() => []);
  for (let i = 0; i < n; i++) byT[iv.t[i]].push(i);
  for (const a of byT) a.sort((x, y) => iv.s[x] - iv.s[y]);
  return {
    n, waitKind, kindIsWait, hours, tok, root, byT, t0, t1,
    kindIndex: new Map(data.kinds.map((k, i) => [k, i])),
    kindTotals: [...totals.entries()].sort((a, b) => b[1] - a[1]),
  };
}

// ---------------------------------------------------------------------------------------------------------- filters
function rangeBounds() {
  const f = S.f, now = Date.now() / 1000;
  if (f.range === "all") return [null, null];
  const m = /^(\d+)([hd])$/.exec(f.range || "");  // 1h, 6h, 12h, 24h, 7d, 30d
  if (m) return [now - +m[1] * (m[2] === "h" ? 3600 : 86400), null];
  if (f.range === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return [d.getTime() / 1000, null]; }
  return [f.from, f.to];
}

// Returns a function weight(i) -> seconds of interval i inside the filter (0 when filtered out).
function makeFilter() {
  const D = S.data, P = S.prep, iv = D.iv, f = S.f;
  const [from, to] = rangeBounds();
  const lo = from ?? -Infinity, hi = to ?? Infinity;
  const search = f.search.trim().toLowerCase();
  const tPass = D.transcripts.map((t) => {
    if (f.session !== "" && P.root[t.idx] !== +f.session) return false;
    if (f.role === "main" && t.role !== "main") return false;
    if (f.role === "sub" && t.role === "main") return false;
    if (search && !(t.description || "").toLowerCase().includes(search)) return false;
    return true;
  });
  const ws = f.ws === "" ? -1 : D.workstreams.indexOf(f.ws);
  const br = f.branch === "" ? -1 : D.branches.indexOf(f.branch);
  const ranged = from != null || to != null;
  const pass = (i) => tPass[iv.t[i]] && (ws < 0 || iv.ws[i] === ws) && (br < 0 || iv.b[i] === br)
    && iv.e[i] >= lo && iv.s[i] <= hi;
  const weight = (i) => {
    if (!pass(i)) return 0;
    const s = iv.s[i], e = iv.e[i], w = iv.w[i];
    if (!ranged || (s >= lo && e <= hi)) return w;
    const h = P.hours.get(i);
    if (h) {
      let sum = 0;
      for (let j = 0; j < h.length; j += 2) {
        const a = Math.max(h[j], lo), b = Math.min(h[j] + 3600, hi);
        if (b > a) sum += h[j + 1] * (b - a) / 3600;
      }
      return sum;
    }
    if (e <= s) return 0;
    return w * Math.max(0, Math.min(e, hi) - Math.max(s, lo)) / (e - s);
  };
  return { pass, weight, tPass, lo, hi, ranged, from, to };
}

// ---------------------------------------------------------------------------------------------------------- aggregate
function aggregate(F) {
  const D = S.data, P = S.prep, iv = D.iv, NOSHARE = D.flags.noshare;
  const byKind = new Map(), byT = new Map(), byWs = new Map(), byBranch = new Map();
  const busySpans = [];
  let agent = 0, waitingTotal = 0, overhead = 0;
  const add = (m, key, kind, w) => {
    let o = m.get(key);
    if (!o) m.set(key, (o = { total: 0, busy: 0, kinds: new Map(), cost: 0, first: Infinity, last: -Infinity, ts: new Set() }));
    o.total += w;
    o.kinds.set(kind, (o.kinds.get(kind) || 0) + w);
    return o;
  };
  for (let i = 0; i < P.n; i++) {
    if (iv.f[i] & NOSHARE) continue;
    const w = F.weight(i);
    if (w <= 0) continue;
    const kind = D.kinds[iv.k[i]], wait = P.kindIsWait[iv.k[i]];
    byKind.set(kind, (byKind.get(kind) || 0) + w);
    const t = iv.t[i];
    const ot = add(byT, t, kind, w);
    if (!wait) {
      agent += w;
      ot.busy += w;
      busySpans.push([Math.max(iv.s[i], F.lo), Math.min(iv.e[i], F.hi)]);
      for (const [m, key] of [[byWs, iv.ws[i]], [byBranch, iv.b[i]]]) {
        const o = add(m, key, kind, w);
        o.busy += w;
        o.first = Math.min(o.first, iv.s[i]);
        o.last = Math.max(o.last, iv.e[i]);
        o.ts.add(t);
      }
    } else if (kind === "overhead") overhead += w; else waitingTotal += w;
  }
  // Busy wall-clock: union of busy intervals over every transcript.
  busySpans.sort((a, b) => a[0] - b[0]);
  let wall = 0, cs = -Infinity, ce = -Infinity;
  for (const [s, e] of busySpans) {
    if (s > ce) { if (ce > cs) wall += ce - cs; cs = s; ce = e; } else if (e > ce) ce = e;
  }
  if (ce > cs) wall += ce - cs;
  // Tokens and cost (per request, counted when its model interval is in the filter, scaled like its time).
  const tk = D.tok, models = new Map();
  let cost = 0, thinkMs = 0, outTok = 0, cacheRead = 0;
  for (let j = 0; j < tk.i.length; j++) {
    const i = tk.i[j];
    if (!F.pass(i)) continue;
    const frac = F.ranged && iv.w[i] > 0 ? Math.min(1, F.weight(i) / iv.w[i]) : 1;
    const m = D.models[tk.m[j]] || "(unknown)";
    let o = models.get(m);
    if (!o) models.set(m, (o = { req: 0, in: 0, out: 0, cr: 0, cw: 0, th: 0, ms: 0, cost: 0 }));
    o.req += frac; o.in += tk.in[j] * frac; o.out += tk.out[j] * frac; o.cr += tk.cr[j] * frac;
    o.cw += (tk.cw5[j] + tk.cw1[j]) * frac; o.th += tk.th[j] * frac; o.ms += tk.ms[j] * frac; o.cost += tk.cost[j] * frac;
    cost += tk.cost[j] * frac; thinkMs += tk.ms[j] * frac; outTok += tk.out[j] * frac; cacheRead += tk.cr[j] * frac;
    for (const [mp, key] of [[byWs, iv.ws[i]], [byBranch, iv.b[i]], [byT, iv.t[i]]]) {
      const o2 = mp.get(key);
      if (o2) o2.cost += tk.cost[j] * frac;
    }
  }
  return { byKind, byT, byWs, byBranch, agent, wall, waitingTotal, overhead, models, cost, thinkMs, outTok, cacheRead };
}

// ---------------------------------------------------------------------------------------------------------- render
let F = null, A = null;
function renderAll() {
  if (!S.data) return;
  F = makeFilter();
  A = aggregate(F);
  renderRangeNote();
  renderTiles();
  renderKinds();
  renderOverTime();
  renderUnreal();
  renderTimelineRows();
  drawTimeline();
  renderWorkstreams();
  renderMains();
  renderSubagents();
  renderSlow();
  renderModels();
  renderBranches();
}

function renderRangeNote() {
  const note = $("#range-note");
  if (S.f.range === "custom" && S.f.from != null) {
    note.hidden = false;
    note.replaceChildren(`Range: ${fmtWhen(S.f.from)} – ${fmtWhen(S.f.to)} (${fmtDur(S.f.to - S.f.from)})`,
      el("button", { class: "ghost", type: "button", onclick: () => { setFilter("range", "all"); } }, "Clear"));
  } else note.hidden = true;
}

function tile(label, value, detail, extra, hero) {
  return el("div", { class: "tile" + (hero ? " hero" : "") }, el("div", { class: "label" }, label),
    el("div", { class: "value" }, value), detail ? el("div", { class: "detail" }, detail) : null, extra || null);
}
function meter(parts) {
  const total = parts.reduce((a, p) => a + p[1], 0) || 1;
  const m = el("div", { class: "meter" });
  for (const [color, v, title] of parts) if (v > 0) m.append(el("i", { style: `width:${(100 * v / total).toFixed(2)}%;background:${color}`, title }));
  return m;
}
function renderTiles() {
  const D = S.data;
  const subs = new Set(), mains = new Set();
  for (const [t, o] of A.byT) if (o.busy > 0) (D.transcripts[t].role === "main" ? mains : subs).add(t);
  let mActive = 0, mUser = 0, mSub = 0, mIdle = 0;
  for (const [t, o] of A.byT) {
    if (D.transcripts[t].role !== "main") continue;
    mActive += o.busy;
    for (const [k, v] of o.kinds) {
      if (k === "waiting on user" || k === "editor open for user") mUser += v;
      else if (k === "waiting on subagent" || k === "waiting on wakeup") mSub += v;
      else if (k === "idle") mIdle += v;
    }
  }
  const model = A.byKind.get("model") || 0;
  const topTool = [...A.byKind.entries()].filter(([k]) => k !== "model" && !S.prep.waitKind.has(k)).sort((a, b) => b[1] - a[1])[0];
  const live = D.transcripts.filter((t) => t.live && F.tPass[t.idx]).length;
  $("#tiles").replaceChildren(
    tile("Agent time", fmtHours(A.agent), `${mains.size} session${mains.size === 1 ? "" : "s"} · ${subs.size} subagents busy`, null, true),
    tile("Busy wall-clock", fmtHours(A.wall), A.wall > 0 ? `${(A.agent / A.wall).toFixed(1)}× parallel on average` : ""),
    tile("Main sessions", fmtHours(mActive) + " active", `waiting: user ${fmtDur(mUser)} · subagents ${fmtDur(mSub)} · idle ${fmtDur(mIdle)}`,
      meter([[palette[1], mActive, "active"], [palette["--wait-user"], mUser, "waiting on user"], [palette["--wait-sub"], mSub, "waiting on subagents"], [palette["--idle"], mIdle, "idle"]])),
    tile("Model", A.agent ? fmtPct(model / A.agent) : "–", `${fmtHours(model)} · thinking ${fmtDur(A.thinkMs / 1000)}`),
    tile(topTool ? `Top tool: ${topTool[0]}` : "Top tool", topTool && A.agent ? fmtPct(topTool[1] / A.agent) : "–", topTool ? fmtHours(topTool[1]) : ""),
    tile("Tokens out", fmtNum(A.outTok), `cache read ${fmtNum(A.cacheRead)}`),
    tile("Estimated cost", fmtMoney(A.cost), A.agent ? `${fmtMoney(A.cost / (A.agent / 3600))} per agent hour` : ""),
    tile("Live", String(live), live ? "transcripts written in the last 5 min" : "nothing running"),
  );
}

function renderKinds() {
  let rows = [...A.byKind.entries()].filter(([k]) => S.includeWaiting || !S.prep.waitKind.has(k)).sort((a, b) => b[1] - a[1]);
  const total = S.includeWaiting ? rows.reduce((a, r) => a + r[1], 0) : A.agent;
  const KEEP = 14;
  let folded = [];
  if (rows.length > KEEP + 1) { folded = rows.slice(KEEP); rows = rows.slice(0, KEEP); rows.push([`${folded.length} more kinds`, folded.reduce((a, r) => a + r[1], 0)]); }
  const max = rows.length ? rows[0][1] : 1;
  const box = el("div", { class: "bars" });
  const split = $("#opt-ue-split").checked ? ueSplitByKind() : new Map();
  for (const [k, v] of rows) {
    const isFold = folded.length && k.endsWith("more kinds");
    const c = isFold ? palette["--other"] : colorOf(k);
    const calls = isFold ? 0 : callsOfKind(k);
    const row = [el("div", { class: "name" }, el("i", { style: `background:${c}` }), k),
      el("div", { class: "track" }, el("b", { style: `width:${(100 * v / max).toFixed(2)}%;background:${c}` })),
      el("div", { class: "num" }, fmtDur(v)), el("div", { class: "pct" }, total ? fmtPct(v / total) : "")];
    for (const n of row) {
      n.addEventListener("mousemove", (ev) => showTip(ev, isFold ? folded.map(([fk, fv]) => ({ k: colorOf(fk), v: fmtDur(fv), m: fk }))
        : [[k, c], fmtDur(v), total ? `${fmtPct(v / total)} of ${S.includeWaiting ? "wall-clock" : "agent time"}` : "", calls ? `${calls} calls` : ""]));
      n.addEventListener("mouseleave", hideTip);
    }
    box.append(...row);
    const parts = split.get(k);
    if (parts) {
      for (const [p, pv] of [...parts].sort((a, b) => b[1] - a[1])) {
        if (pv / (total || 1) < 0.001) continue;
        addBarRow(box, p === "outside the editor" ? "outside the editor (lock waits, launch, …)" : UE_PHASES.includes(p) ? `in editor: ${p}` : p, pv, total || 1, max, c, true);
      }
    }
  }
  $("#kinds").replaceChildren(rows.length ? box : el("div", { class: "sub" }, "Nothing in this filter."));
}
function callsOfKind(kind) {
  const D = S.data, iv = D.iv, ki = S.prep.kindIndex.get(kind);
  if (kind === "model") return D.tok.i.filter((i) => F.pass(i)).length;
  if (S.prep.waitKind.has(kind)) return 0;
  const noTool = D.tools.indexOf("");
  let c = 0;
  for (let i = 0; i < S.prep.n; i++) if (iv.k[i] === ki && iv.n[i] !== noTool && F.pass(i)) c++;
  return c;
}

// Stacked columns per 5 / 15 min, hour or day, top kinds + Other.
function renderOverTime() {
  const D = S.data, P = S.prep, iv = D.iv, NOSHARE = D.flags.noshare;
  const lo = F.from ?? P.t0, hi = F.to ?? Math.min(P.t1, Date.now() / 1000);
  let bucket = S.bucket;
  if (bucket === "auto") bucket = hi - lo <= 2.5 * 3600 ? "5min" : hi - lo <= 13 * 3600 ? "15min" : hi - lo <= 4 * 86400 ? "hour" : "day";
  const fine = bucket === "5min" ? 300 : bucket === "15min" ? 900 : 0;  // below an hour: spread each interval evenly
  const step = fine || (bucket === "hour" ? 3600 : 86400);
  const keyOf = bucket !== "day" ? (t) => Math.floor(t / step) * step : (t) => { const d = new Date(t * 1000); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
  const series = new Map();
  const shown = (k) => (S.includeWaiting || !P.waitKind.has(k));
  const kindName = (k) => (isOther(k) && !P.waitKind.has(k) ? "Other" : k);
  for (let i = 0; i < P.n; i++) {
    if (iv.f[i] & NOSHARE) continue;
    const kind = D.kinds[iv.k[i]];
    if (!shown(kind)) continue;
    const w = F.weight(i);
    if (w <= 0) continue;
    const name = kindName(kind);
    const h = P.hours.get(i);
    const addAt = (t, v) => { const key = keyOf(t); let m = series.get(key); if (!m) series.set(key, (m = new Map())); m.set(name, (m.get(name) || 0) + v); };
    if (fine) {
      const a = Math.max(iv.s[i], F.lo ?? -Infinity), b = Math.min(iv.e[i], F.hi ?? Infinity);
      if (b - a <= 0) { addAt(a, w); continue; }
      for (let t = keyOf(a); t < b; t += step) addAt(t + 1, w * (Math.min(b, t + step) - Math.max(a, t)) / (b - a));
    } else if (h) {
      const scale = w / (iv.w[i] || 1);
      for (let j = 0; j < h.length; j += 2) if (h[j] + 3600 > F.lo && h[j] < F.hi) addAt(h[j] + 1, h[j + 1] * Math.min(1, scale));
    } else addAt(Math.max(iv.s[i], F.lo), w);
  }
  const keys = [];
  for (let t = keyOf(lo); t <= hi; t = bucket !== "day" ? t + step : keyOf(t + 86400 + 7200)) keys.push(t);
  if (keys.length > 2000) keys.splice(0, keys.length - 2000);
  const order = [];
  const totals = new Map();
  for (const m of series.values()) for (const [k, v] of m) totals.set(k, (totals.get(k) || 0) + v);
  const slotOf = (k) => (S.colors[S.project] || {})[k] || 99;
  order.push(...[...totals.keys()].sort((a, b) => (WAIT_COLORS[a] ? 1 : 0) - (WAIT_COLORS[b] ? 1 : 0) || slotOf(a) - slotOf(b) || totals.get(b) - totals.get(a)));
  const colorName = (k) => (k === "Other" ? palette["--other"] : colorOf(k));

  const W = Math.max(320, $("#overtime").clientWidth || 600), H = 240, m = { l: 44, r: 8, t: 8, b: 26 };
  const maxV = Math.max(1, ...keys.map((k) => [...(series.get(k)?.values() || [])].reduce((a, b) => a + b, 0)));
  const unit = maxV > 5400 ? 3600 : 60, unitName = unit === 3600 ? "h" : "m";
  const tickStep = niceMax(maxV / unit / 4), nice = tickStep * 4, yMax = nice * unit;
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Agent time per " + bucket });
  const g = svg("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const y = m.t + (H - m.t - m.b) * (1 - i / 4);
    g.append(svg("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }));
    const tx = svg("text", { x: m.l - 6, y: y + 4, "text-anchor": "end" }); tx.textContent = `${+(tickStep * i).toFixed(2)}${unitName}`; g.append(tx);
  }
  root.append(g);
  const bw = (W - m.l - m.r) / Math.max(keys.length, 1);
  const barW = Math.max(1, Math.min(24, bw - 2));
  const every = Math.max(1, Math.ceil(70 / bw));
  keys.forEach((k, idx) => {
    const x = m.l + idx * bw + (bw - barW) / 2;
    let y = H - m.b;
    const col = series.get(k);
    const segs = [];
    if (col) for (const name of order) {
      const v = col.get(name) || 0;
      if (v <= 0) continue;
      const h = (H - m.t - m.b) * v / yMax;
      segs.push([name, v, y - h, h]);
      y -= h;
    }
    segs.forEach(([name, , top, h], si) => {
      const last = si === segs.length - 1;
      const gap = last ? 0 : Math.min(2, h / 3);
      root.append(svg(last && h > 4 ? "path" : "rect", last && h > 4
        ? { d: roundTop(x, top, barW, h, Math.min(4, barW / 2)), fill: colorName(name) }
        : { x, y: top + gap, width: barW, height: Math.max(0.5, h - gap), fill: colorName(name) }));
    });
    const hit = svg("rect", { x: m.l + idx * bw, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent" });
    hit.addEventListener("mousemove", (ev) => {
      const lines = [bucket !== "day" ? `${fmtDay(k)} ${fmtTime(k)}–${fmtTime(k + step)}` : fmtDay(k)];
      const tot = segs.reduce((a, s) => a + s[1], 0);
      lines.push({ v: fmtDur(tot), m: "total" });
      for (const [name, v] of [...segs].reverse()) lines.push({ k: colorName(name), v: fmtDur(v), m: name });
      showTip(ev, lines);
    });
    hit.addEventListener("mouseleave", hideTip);
    hit.addEventListener("click", () => setCustomRange(k, k + step));
    root.append(hit);
    if (idx % every === 0) {
      const tx = svg("text", { x: m.l + idx * bw + bw / 2, y: H - 8, "text-anchor": "middle", class: "axis" });
      const d = new Date(k * 1000);
      tx.textContent = bucket === "day" ? `${d.getDate()} ${MON[d.getMonth()]}` : fine ? fmtTime(k) : (d.getHours() === 0 || idx === 0 ? `${d.getDate()} ${MON[d.getMonth()]}` : `${pad2(d.getHours())}:00`);
      tx.setAttribute("fill", palette["--text-muted"]); tx.setAttribute("font-size", "11");
      root.append(tx);
    }
  });
  root.append(svg("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: palette["--axis"] }));
  $("#overtime").replaceChildren(root);
  $("#overtime-legend").replaceChildren(...order.map((k) => el("span", {}, el("i", { style: `background:${colorName(k)}` }), k)));
}
function niceMax(v) {
  const steps = [1, 2, 2.5, 5, 10];
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(v, 1e-9))));
  for (const s of steps) if (s * p >= v) return s * p;
  return 10 * p;
}
function roundTop(x, y, w, h, r) {
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// ---------------------------------------------------------------------------------------------------------- tables
function table(container, cols, rows, opts = {}) {
  const st = (container._sort = container._sort || { key: opts.sort, dir: -1 });
  const sorted = [...rows];
  const col = cols.find((c) => c.key === st.key);
  if (col) sorted.sort((a, b) => { const x = col.sortv ? col.sortv(a) : a[col.key], y = col.sortv ? col.sortv(b) : b[col.key]; return (x > y ? 1 : x < y ? -1 : 0) * st.dir; });
  const thead = el("tr", {}, ...cols.map((c) => el("th", {
    class: c.num ? "num" : "", onclick: () => { if (st.key === c.key) st.dir *= -1; else { st.key = c.key; st.dir = c.num ? -1 : 1; } table(container, cols, rows, opts); },
  }, c.label + (st.key === c.key ? (st.dir < 0 ? " ↓" : " ↑") : ""))));
  const body = sorted.slice(0, opts.limit || 500).map((r) => {
    const tr = el("tr", { class: opts.onclick ? "click" : "" }, ...cols.map((c) => {
      const v = c.render ? c.render(r) : r[c.key];
      return el("td", { class: (c.num ? "num " : "") + (c.cls || "") , title: c.title ? c.title(r) : "" }, v);
    }));
    if (opts.onclick) tr.addEventListener("click", () => opts.onclick(r));
    return tr;
  });
  container.replaceChildren(rows.length ? el("table", {}, el("thead", {}, thead), el("tbody", {}, ...body)) : el("div", { class: "sub" }, "Nothing in this filter."));
}
function mini(kinds, width = 160) {
  const total = [...kinds.values()].reduce((a, b) => a + b, 0) || 1;
  const box = el("div", { class: "mini", style: `width:${width}px` });
  const parts = [...kinds.entries()].sort((a, b) => ((S.colors[S.project] || {})[a[0]] || 99) - ((S.colors[S.project] || {})[b[0]] || 99) || b[1] - a[1]);
  for (const [k, v] of parts) if (v / total > 0.004) box.append(el("i", { style: `width:${(100 * v / total).toFixed(2)}%;background:${colorOf(k)}`, title: `${k} ${fmtDur(v)}` }));
  return box;
}
const swatch = (k) => el("span", {}, el("span", { class: "sw", style: `background:${colorOf(k)}` }), k);
const busyKinds = (o) => new Map([...o.kinds].filter(([k]) => !S.prep.waitKind.has(k)));
const topKinds = (o, n = 3) => [...busyKinds(o)].sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => `${k} ${fmtDur(v)}`).join(", ");

function renderWorkstreams() {
  const D = S.data;
  const rows = [...A.byWs].filter(([, o]) => o.busy > 0).map(([ws, o]) => ({
    name: D.workstreams[ws], busy: o.busy, o, cost: o.cost, agents: [...o.ts].filter((t) => D.transcripts[t].role !== "main").length,
    span: o.last - o.first,
  }));
  table($("#workstreams"), [
    { key: "name", label: "Workstream", cls: "label" },
    { key: "busy", label: "Agent time", num: true, render: (r) => fmtDur(r.busy) },
    { key: "share", label: "Share", num: true, sortv: (r) => r.busy, render: (r) => fmtPct(r.busy / (A.agent || 1)) },
    { key: "mix", label: "Mix", sortv: (r) => r.busy, render: (r) => mini(busyKinds(r.o), 140) },
    { key: "agents", label: "Subagents", num: true },
    { key: "cost", label: "Cost", num: true, render: (r) => fmtMoney(r.cost) },
  ], rows, { sort: "busy", onclick: (r) => setFilter("ws", S.f.ws === r.name ? "" : r.name) });
}

function renderMains() {
  const D = S.data;
  const rows = [];
  for (const [t, o] of A.byT) {
    const tr = D.transcripts[t];
    if (tr.role !== "main") continue;
    const g = (names) => names.reduce((a, k) => a + (o.kinds.get(k) || 0), 0);
    rows.push({ t, title: tr.title, active: o.busy, user: g(["waiting on user", "editor open for user"]), sub: g(["waiting on subagent", "waiting on wakeup"]),
      idle: g(["idle"]), overhead: g(["overhead"]), live: tr.live, cost: o.cost });
  }
  table($("#mains"), [
    { key: "title", label: "Session", cls: "label", render: (r) => el("span", {}, r.title, r.live ? el("span", { class: "flag" }, "live") : null) },
    { key: "active", label: "Active", num: true, render: (r) => fmtDur(r.active) },
    { key: "user", label: "On user", num: true, render: (r) => fmtDur(r.user) },
    { key: "sub", label: "On subagents", num: true, render: (r) => fmtDur(r.sub) },
    { key: "idle", label: "Idle", num: true, render: (r) => fmtDur(r.idle) },
    { key: "bar", label: "", sortv: (r) => r.active, render: (r) => mini(new Map([["model", r.active], ["waiting on user", r.user], ["waiting on subagent", r.sub], ["idle", r.idle]]), 120) },
  ], rows, { sort: "active", onclick: (r) => setFilter("session", String(r.t)) });
}

function renderSubagents() {
  const D = S.data;
  const rows = [];
  for (const [t, o] of A.byT) {
    const tr = D.transcripts[t];
    if (tr.role === "main" || o.busy <= 0) continue;
    rows.push({ t, name: tr.description || tr.id, ws: tr.workstream, session: D.transcripts[tr.parent ?? t]?.title || "", busy: o.busy,
      wall: tr.end - tr.start, o, model: Object.keys(tr.models || {})[0] || "", cost: o.cost, start: tr.start,
      kind: tr.role === "workflow" ? `workflow ${tr.phase || ""}` : (tr.agentType || "") });
  }
  table($("#subagents"), [
    { key: "name", label: "Subagent", cls: "label", title: (r) => r.name },
    { key: "ws", label: "Workstream" },
    { key: "busy", label: "Busy", num: true, render: (r) => fmtDur(r.busy) },
    { key: "wall", label: "Wall", num: true, render: (r) => fmtDur(r.wall) },
    { key: "mix", label: "Mix", sortv: (r) => r.busy, render: (r) => mini(busyKinds(r.o), 140) },
    { key: "top", label: "Top kinds", cls: "label", sortv: (r) => r.busy, render: (r) => topKinds(r.o) },
    { key: "model", label: "Model", render: (r) => r.model.replace(/^claude-/, "") },
    { key: "cost", label: "Cost", num: true, render: (r) => fmtMoney(r.cost) },
    { key: "start", label: "Started", num: true, render: (r) => fmtWhen(r.start) },
  ], rows, { sort: "busy", onclick: (r) => focusTranscript(r.t) });
}

function renderSlow() {
  const D = S.data, P = S.prep, iv = D.iv, FL = D.flags;
  const sel = $("#opt-slow-kind");
  const kinds = [...A.byKind.keys()].filter((k) => !P.waitKind.has(k) && k !== "model" && k !== "compaction").sort();
  const cur = S.slowKind;
  sel.replaceChildren(el("option", { value: "" }, "All tool kinds"), ...kinds.map((k) => el("option", { value: k }, k)));
  sel.value = kinds.includes(cur) ? cur : "";
  const want = sel.value ? P.kindIndex.get(sel.value) : -1;
  const cand = [];
  for (let i = 0; i < P.n; i++) {
    const ki = iv.k[i];
    if (P.kindIsWait[ki] || iv.f[i] & FL.noshare || iv.n[i] === D.tools.indexOf("") ) continue;
    const kind = D.kinds[ki];
    if (kind === "model" || kind === "compaction" || (want >= 0 && ki !== want)) continue;
    if (!F.pass(i)) continue;
    cand.push(i);
  }
  cand.sort((a, b) => (iv.e[b] - iv.s[b]) - (iv.e[a] - iv.s[a]));
  const rows = cand.slice(0, 300).map((i) => {
    const t = D.transcripts[iv.t[i]];
    const fl = iv.f[i];
    return { i, dur: iv.e[i] - iv.s[i], share: iv.w[i], kind: D.kinds[iv.k[i]], label: D.labels[iv.l[i]] || D.tools[iv.n[i]],
      where: t.role === "main" ? t.title : t.description, start: iv.s[i],
      flags: [fl & FL.background ? "background" : "", fl & FL.unfinished ? "no result" : "", fl & FL.error ? "error" : ""].filter(Boolean) };
  });
  table($("#slow"), [
    { key: "dur", label: "Duration", num: true, render: (r) => fmtDur(r.dur) },
    { key: "share", label: "Counted", num: true, title: () => "time attributed after splitting overlaps with concurrent calls", render: (r) => fmtDur(r.share) },
    { key: "kind", label: "Kind", render: (r) => swatch(r.kind) },
    { key: "label", label: "Call", cls: "label", title: (r) => r.label, render: (r) => el("span", {}, r.label, ...r.flags.map((f) => el("span", { class: "flag" }, f))) },
    { key: "where", label: "Agent", cls: "label", title: (r) => r.where },
    { key: "start", label: "Started", num: true, render: (r) => fmtWhen(r.start) },
  ], rows, { sort: "dur", onclick: (r) => focusInterval(r.i) });
}

function renderModels() {
  const rows = [...A.models].map(([m, o]) => ({ m, ...o }));
  const tot = rows.reduce((a, r) => { for (const k of ["req", "in", "out", "cr", "cw", "th", "ms", "cost"]) a[k] = (a[k] || 0) + r[k]; return a; }, { m: "Total" });
  if (rows.length > 1) rows.push({ ...tot, total: true });
  const reported = S.data.transcripts.filter((t) => t.role === "main" && F.tPass[t.idx] && t.costStates.length)
    .reduce((a, t) => a + Math.max(...t.costStates.map((c) => c.totalCostUSD || 0)), 0);
  $("#models-note").textContent = reported ? `Claude Code's own cost-state snapshots (written on resume; cover side requests the transcripts don't show) report ${fmtMoney(reported)} for the sessions that have one.` : "";
  table($("#models"), [
    { key: "m", label: "Model", render: (r) => (r.total ? el("b", {}, "Total") : r.m.replace(/^claude-/, "")) },
    { key: "req", label: "Requests", num: true, render: (r) => fmtNum(r.req) },
    { key: "out", label: "Output", num: true, render: (r) => fmtNum(r.out) },
    { key: "th", label: "Thinking tok", num: true, render: (r) => fmtNum(r.th) },
    { key: "ms", label: "Thinking time", num: true, render: (r) => fmtDur(r.ms / 1000) },
    { key: "cr", label: "Cache read", num: true, render: (r) => fmtNum(r.cr) },
    { key: "cw", label: "Cache write", num: true, render: (r) => fmtNum(r.cw) },
    { key: "in", label: "Input", num: true, render: (r) => fmtNum(r.in) },
    { key: "cost", label: "Cost", num: true, render: (r) => fmtMoney(r.cost) },
  ], rows, { sort: "cost" });
}

function renderBranches() {
  const D = S.data;
  const rows = [...A.byBranch].filter(([, o]) => o.busy > 0).map(([b, o]) => ({ name: D.branches[b] || "(none)", busy: o.busy, o, cost: o.cost, last: o.last }));
  table($("#branches"), [
    { key: "name", label: "Branch", cls: "label" },
    { key: "busy", label: "Agent time", num: true, render: (r) => fmtDur(r.busy) },
    { key: "mix", label: "Mix", sortv: (r) => r.busy, render: (r) => mini(busyKinds(r.o), 140) },
    { key: "cost", label: "Cost", num: true, render: (r) => fmtMoney(r.cost) },
    { key: "last", label: "Last active", num: true, render: (r) => fmtWhen(r.last) },
  ], rows, { sort: "busy", onclick: (r) => setFilter("branch", S.f.branch === r.name ? "" : r.name) });
}

// ---------------------------------------------------------------------------------------------------------- timeline
const ROW_H = 18, BAR_H = 12, LABEL_W = 260;
function renderTimelineRows() {
  const D = S.data, P = S.prep;
  const rows = [];
  const mains = D.transcripts.filter((t) => t.role === "main").sort((a, b) => a.start - b.start);
  const kids = new Map();
  for (const t of D.transcripts) if (t.role !== "main") { const p = t.parent ?? -1; if (!kids.has(p)) kids.set(p, []); kids.get(p).push(t); }
  const visible = (t) => F.tPass[t.idx] && t.end >= F.lo && t.start <= F.hi && P.byT[t.idx].some((i) => F.pass(i));
  for (const m of mains) {
    const ks = (kids.get(m.idx) || []).filter(visible).sort((a, b) => a.start - b.start);
    if (visible(m)) rows.push({ t: m.idx, depth: 0 });
    for (const k of ks) rows.push({ t: k.idx, depth: 1 });
  }
  for (const k of (kids.get(-1) || []).filter(visible)) rows.push({ t: k.idx, depth: 1 });
  S.tl.rows = rows;
  const canvas = $("#tl-rows");
  canvas.style.height = `${Math.max(rows.length * ROW_H + 4, 40)}px`;
  const legendKinds = [...(Object.entries(S.colors[S.project] || {}))].sort((a, b) => a[1] - b[1]).map(([k]) => k).filter((k) => A.byKind.has(k));
  const extra = ["Other", "waiting on user", "waiting on subagent", "idle"].filter((k) => k === "Other" || A.byKind.has(k));
  $("#tl-legend").replaceChildren(...[...legendKinds, ...extra].map((k) => el("span", {}, el("i", { style: `background:${k === "Other" ? palette["--other"] : colorOf(k)}` }), k)),
    el("span", {}, el("i", { style: `background:${palette["--text-muted"]};height:3px` }), "background subagent"));
}
function fitTimeline() {
  const P = S.prep;
  const lo = F && F.from != null ? F.from : P.t0, hi = F && F.to != null ? F.to : P.t1;
  const pad = (hi - lo) * 0.01 + 60;
  S.tl.v0 = lo - pad; S.tl.v1 = hi + pad;
}
function setupCanvas(c) {
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, w, h];
}
const TICKS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
let drawPending = false;
function drawTimeline() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; drawAxis(); drawRows(); });
}
function xOf(t, w) { return LABEL_W + (t - S.tl.v0) / (S.tl.v1 - S.tl.v0) * (w - LABEL_W - 8); }
function tOf(x, w) { return S.tl.v0 + (x - LABEL_W) / (w - LABEL_W - 8) * (S.tl.v1 - S.tl.v0); }
function drawAxis() {
  const D = S.data, P = S.prep, iv = D.iv, NOSHARE = D.flags.noshare;
  const [ctx, w, h] = setupCanvas($("#tl-axis"));
  ctx.clearRect(0, 0, w, h);
  const span = S.tl.v1 - S.tl.v0;
  const px = (w - LABEL_W - 8);
  const step = TICKS.find((s) => s / span * px >= 80) || 604800;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textBaseline = "top";
  const off = new Date().getTimezoneOffset() * 60;
  let t = Math.ceil((S.tl.v0 - off) / step) * step + off;
  if (step >= 86400) { const d = new Date(S.tl.v0 * 1000); d.setHours(0, 0, 0, 0); t = d.getTime() / 1000; }
  for (; t < S.tl.v1; t += step) {
    const x = xOf(t, w);
    if (x < LABEL_W) continue;
    ctx.fillStyle = palette["--grid"]; ctx.fillRect(Math.round(x), 16, 1, h - 16);
    const d = new Date(t * 1000);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
    ctx.fillStyle = midnight || step >= 86400 ? palette["--text-secondary"] : palette["--text-muted"];
    ctx.fillText(step >= 86400 || midnight ? fmtDay(t) : fmtTime(t), x + 3, 2);
  }
  // Concurrency lane: average number of busy agents per pixel column, stacked by kind.
  const laneTop = 20, laneH = h - laneTop - 4;
  const cols = Math.max(1, Math.floor(px));
  const kindsOrder = [];
  const kmap = new Map();
  const diff = new Map();
  for (let i = 0; i < P.n; i++) {
    if (iv.f[i] & NOSHARE || P.kindIsWait[iv.k[i]]) continue;
    if (iv.e[i] < S.tl.v0 || iv.s[i] > S.tl.v1 || !F.pass(i)) continue;
    const kind = D.kinds[iv.k[i]];
    const name = isOther(kind) ? "Other" : kind;
    let arr = diff.get(name);
    if (!arr) { arr = new Float64Array(cols + 2); diff.set(name, arr); kindsOrder.push(name); kmap.set(name, name === "Other" ? palette["--other"] : colorOf(kind)); }
    const a = Math.max(0, (Math.max(iv.s[i], F.lo) - S.tl.v0) / span * cols), b = Math.min(cols, (Math.min(iv.e[i], F.hi) - S.tl.v0) / span * cols);
    if (b <= a) continue;
    // fractional coverage: add 1 per column covered, partial at the ends
    const ia = Math.floor(a), ib = Math.floor(b);
    if (ia === ib) { arr[ia] += b - a; arr[ia + 1] -= b - a; continue; }
    arr[ia] += ia + 1 - a; arr[ia + 1] -= ia + 1 - a;
    arr[ia + 1] += 1; arr[ib] -= 1;
    arr[ib] += b - ib; arr[ib + 1] -= b - ib;
  }
  const slotOf = (k) => (S.colors[S.project] || {})[k] || 99;
  kindsOrder.sort((a, b) => slotOf(a) - slotOf(b));
  const series = kindsOrder.map((k) => { const d = diff.get(k), out = new Float64Array(cols); let run = 0; for (let c = 0; c < cols; c++) { run += d[c]; out[c] = run; } return out; });
  let maxC = 1;
  for (let c = 0; c < cols; c++) { let s = 0; for (const a of series) s += a[c]; if (s > maxC) maxC = s; }
  maxC = Math.ceil(maxC);
  S.tl.lane = { series, kindsOrder, cols, maxC, laneTop, laneH };
  for (let c = 0; c < cols; c++) {
    let y = laneTop + laneH;
    for (let s = 0; s < series.length; s++) {
      const v = series[s][c];
      if (v <= 0.001) continue;
      const hh = v / maxC * laneH;
      ctx.fillStyle = kmap.get(kindsOrder[s]);
      ctx.fillRect(LABEL_W + c, y - hh, 1.02, hh);
      y -= hh;
    }
  }
  ctx.fillStyle = palette["--text-secondary"]; ctx.textBaseline = "middle";
  ctx.fillText("Agents busy", 8, laneTop + laneH / 2 - 7);
  ctx.fillStyle = palette["--text-muted"];
  ctx.fillText(`peak ${maxC}`, 8, laneTop + laneH / 2 + 8);
  ctx.fillStyle = palette["--axis"]; ctx.fillRect(LABEL_W, laneTop + laneH, px, 1);
  if (S.tl.brush) {
    const [a, b] = S.tl.brush;
    ctx.fillStyle = "rgba(42,120,214,0.15)";
    ctx.fillRect(Math.min(a, b), 16, Math.abs(b - a), h - 16);
  }
}
function drawRows() {
  const D = S.data, P = S.prep, iv = D.iv, FL = D.flags;
  const canvas = $("#tl-rows");
  const [ctx, w, h] = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const scroll = $("#tl-scroll");
  const top = scroll.scrollTop, bottom = top + scroll.clientHeight;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  const minW = 0.6;
  const modelKi = P.kindIndex.get("model");
  S.tl.rows.forEach((row, r) => {
    const y = r * ROW_H + 2;
    if (y + ROW_H < top - 50 || y > bottom + 50) return;
    const t = D.transcripts[row.t];
    if (S.tl.highlight === row.t) { ctx.fillStyle = palette["--surface-2"]; ctx.fillRect(0, y - 1, w, ROW_H); }
    // label
    ctx.fillStyle = row.depth ? palette["--text-secondary"] : palette["--text-primary"];
    ctx.font = row.depth ? "12px system-ui, sans-serif" : "600 12px system-ui, sans-serif";
    const label = row.depth ? (t.description || t.id) : t.title;
    ctx.save(); ctx.beginPath(); ctx.rect(0, y, LABEL_W - 8, ROW_H); ctx.clip();
    ctx.fillText(label, 8 + row.depth * 14, y + ROW_H / 2 - 1);
    ctx.restore();
    // span
    const xs = Math.max(LABEL_W, xOf(t.start, w)), xe = Math.min(w - 8, xOf(t.end, w));
    if (xe > xs) { ctx.fillStyle = palette["--grid"]; ctx.fillRect(xs, y + ROW_H / 2 - 0.5, xe - xs, 1); }
    ctx.save(); ctx.beginPath(); ctx.rect(LABEL_W, y - 2, w - LABEL_W - 8, ROW_H + 4); ctx.clip();
    const list = P.byT[row.t];
    const by = y + (ROW_H - BAR_H) / 2 - 1;
    for (let pass = 0; pass < 3; pass++) {
      for (const i of list) {
        if (iv.e[i] < S.tl.v0 || iv.s[i] > S.tl.v1) continue;
        const ki = iv.k[i], wait = P.kindIsWait[ki];
        const bgAgent = iv.f[i] & FL.noshare;
        if (pass === 0 && !wait) continue;
        if (pass === 1 && (wait || bgAgent)) continue;
        if (pass === 2 && !bgAgent) continue;
        if (wait && !S.tl.waiting) continue;
        if (ki === modelKi && !S.tl.model) continue;
        if (!F.pass(i)) continue;
        const kind = D.kinds[ki];
        if (kind === "overhead") continue;
        const x0 = xOf(iv.s[i], w), x1 = xOf(iv.e[i], w);
        const ww = Math.max(minW, x1 - x0);
        if (pass === 2) { ctx.fillStyle = palette["--text-muted"]; ctx.fillRect(x0, by + BAR_H + 1, ww, 2); continue; }
        ctx.fillStyle = colorOf(kind);
        if (pass === 0) ctx.fillRect(x0, by + 3, ww, BAR_H - 6);
        else if (ki === modelKi) ctx.fillRect(x0, by + 2, ww, BAR_H - 4);
        else ctx.fillRect(x0, by, ww, BAR_H);
      }
    }
    ctx.restore();
  });
  // brush overlay
  if (S.tl.brush) {
    const [a, b] = S.tl.brush;
    ctx.fillStyle = "rgba(42,120,214,0.12)";
    ctx.fillRect(Math.min(a, b), 0, Math.abs(b - a), h);
  }
  // now line
  const now = Date.now() / 1000;
  if (now > S.tl.v0 && now < S.tl.v1) { ctx.fillStyle = palette["--accent"]; ctx.fillRect(xOf(now, w), 0, 1, h); }
}

function hitTest(x, y) {
  const D = S.data, P = S.prep, iv = D.iv, FL = D.flags;
  const r = Math.floor((y - 2) / ROW_H);
  const row = S.tl.rows[r];
  if (!row) return null;
  if (x < LABEL_W) return { row };
  const w = $("#tl-rows").clientWidth;
  const t = tOf(x, w), tol = (S.tl.v1 - S.tl.v0) / (w - LABEL_W) * 3;
  const yIn = (y - 2) - r * ROW_H;
  let best = null, bestScore = Infinity;
  const modelKi = P.kindIndex.get("model");
  for (const i of P.byT[row.t]) {
    if (iv.s[i] - tol > t) break;
    if (iv.e[i] + tol < t || !F.pass(i)) continue;
    const kind = D.kinds[iv.k[i]];
    if (kind === "overhead") continue;
    const bgAgent = iv.f[i] & FL.noshare;
    if (bgAgent && yIn < ROW_H - 5) continue;
    if (P.kindIsWait[iv.k[i]] && !S.tl.waiting) continue;
    if (iv.k[i] === modelKi && !S.tl.model) continue;
    // prefer tool calls over model over waiting, then the shortest
    const pri = bgAgent ? 0 : P.kindIsWait[iv.k[i]] ? 3 : iv.k[i] === modelKi ? 2 : 1;
    const score = pri * 1e12 + (iv.e[i] - iv.s[i]);
    if (score < bestScore) { bestScore = score; best = i; }
  }
  return { row, i: best };
}
function tipForInterval(i) {
  const D = S.data, iv = D.iv, FL = D.flags;
  const t = D.transcripts[iv.t[i]];
  const kind = D.kinds[iv.k[i]];
  const lines = [{ k: colorOf(kind), v: fmtDur(iv.e[i] - iv.s[i]), m: kind }];
  const label = D.labels[iv.l[i]];
  const tool = D.tools[iv.n[i]];
  if (label || tool) lines.push(tool && label && tool !== "Bash" && tool !== "PowerShell" ? `${tool}: ${label}` : label || tool);
  if (Math.abs(iv.w[i] - (iv.e[i] - iv.s[i])) > 1 && !(iv.f[i] & FL.noshare)) lines.push({ m: `counted ${fmtDur(iv.w[i])} after sharing overlap` });
  const j = S.prep.tok.get(i);
  if (j != null) {
    const tk = D.tok;
    lines.push({ m: `${(D.models[tk.m[j]] || "").replace(/^claude-/, "")} · out ${fmtNum(tk.out[j])} · cache ${fmtNum(tk.cr[j])} · ${fmtMoney(tk.cost[j])}${tk.ms[j] ? ` · thinking ${fmtDur(tk.ms[j] / 1000)}` : ""}` });
  }
  lines.push({ m: `${fmtWhen(iv.s[i])} → ${fmtTime(iv.e[i])}` });
  lines.push({ m: `${t.role === "main" ? t.title : t.description} · ${D.workstreams[iv.ws[i]]} · ${D.branches[iv.b[i]] || ""}` });
  const fl = iv.f[i];
  lines.push(...ueTipLines(i));
  const flags = [fl & FL.background ? "background" : "", fl & FL.unfinished ? "no result recorded (interrupted, crashed or still running)" : "", fl & FL.error ? "error" : ""].filter(Boolean);
  if (flags.length) lines.push({ m: flags.join(" · ") });
  return lines;
}
function setupTimeline() {
  const rowsC = $("#tl-rows"), axisC = $("#tl-axis"), scroll = $("#tl-scroll");
  let drag = null;
  const onWheel = (ev) => {
    ev.preventDefault();
    const w = rowsC.clientWidth;
    const rect = ev.currentTarget.getBoundingClientRect();
    const x = Math.max(LABEL_W, ev.clientX - rect.left);
    const t = tOf(x, w);
    const k = Math.exp((ev.deltaY || ev.deltaX) * 0.0015);
    const span = Math.max(30, Math.min((S.prep.t1 - S.prep.t0) * 3 + 86400, (S.tl.v1 - S.tl.v0) * k));
    const f = (t - S.tl.v0) / (S.tl.v1 - S.tl.v0);
    S.tl.v0 = t - f * span; S.tl.v1 = S.tl.v0 + span;
    drawTimeline();
  };
  const down = (ev) => {
    const rect = ev.currentTarget.getBoundingClientRect();
    drag = { x: ev.clientX, v0: S.tl.v0, v1: S.tl.v1, shift: ev.shiftKey, x0: ev.clientX - rect.left, moved: false };
    if (drag.shift) S.tl.brush = [drag.x0, drag.x0];
    ev.currentTarget.setPointerCapture(ev.pointerId);
  };
  const move = (ev) => {
    const target = ev.currentTarget;
    const w = rowsC.clientWidth;
    if (drag) {
      const dx = ev.clientX - drag.x;
      if (Math.abs(dx) > 2) drag.moved = true;
      if (drag.shift) { S.tl.brush = [drag.x0, drag.x0 + dx]; }
      else { const dt = dx / (w - LABEL_W - 8) * (drag.v1 - drag.v0); S.tl.v0 = drag.v0 - dt; S.tl.v1 = drag.v1 - dt; }
      hideTip();
      drawTimeline();
      return;
    }
    if (target === rowsC) {
      const rect = rowsC.getBoundingClientRect();
      const hit = hitTest(ev.clientX - rect.left, ev.clientY - rect.top);
      if (hit && hit.i != null) showTip(ev, tipForInterval(hit.i));
      else if (hit && hit.row) {
        const t = S.data.transcripts[hit.row.t];
        const o = A.byT.get(hit.row.t);
        showTip(ev, [t.role === "main" ? t.title : t.description, { v: fmtDur(o ? o.busy : 0), m: "busy in filter" },
          { m: `${fmtWhen(t.start)} → ${fmtWhen(t.end)}` }, { m: [t.agentType, t.workstream, t.branch].filter(Boolean).join(" · ") }]);
      } else hideTip();
    } else {
      const rect = axisC.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const L = S.tl.lane;
      if (L && x >= LABEL_W) {
        const c = Math.floor(x - LABEL_W);
        const lines = [fmtWhen(tOf(x, w))];
        let tot = 0;
        L.series.forEach((s, k) => { if (s[c] > 0.01) { tot += s[c]; } });
        lines.push({ v: tot.toFixed(1), m: "agents busy" });
        L.series.forEach((s, k) => { if (s[c] > 0.01) lines.push({ k: L.kindsOrder[k] === "Other" ? palette["--other"] : colorOf(L.kindsOrder[k]), v: s[c].toFixed(1), m: L.kindsOrder[k] }); });
        showTip(ev, lines);
      } else hideTip();
    }
  };
  const up = (ev) => {
    if (!drag) return;
    const w = rowsC.clientWidth;
    if (drag.shift && S.tl.brush) {
      const [a, b] = S.tl.brush.map((x) => tOf(x, w));
      S.tl.brush = null;
      if (Math.abs(b - a) > 1) setCustomRange(Math.min(a, b), Math.max(a, b));
    } else if (!drag.moved && ev.currentTarget === rowsC) {
      const rect = rowsC.getBoundingClientRect();
      const hit = hitTest(ev.clientX - rect.left, ev.clientY - rect.top);
      S.tl.highlight = hit && hit.row ? (S.tl.highlight === hit.row.t ? null : hit.row.t) : null;
    }
    drag = null;
    drawTimeline();
  };
  for (const c of [rowsC, axisC]) {
    c.addEventListener("wheel", onWheel, { passive: false });
    c.addEventListener("pointerdown", down);
    c.addEventListener("pointermove", move);
    c.addEventListener("pointerup", up);
    c.addEventListener("pointerleave", hideTip);
    c.addEventListener("dblclick", () => { fitTimeline(); drawTimeline(); });
  }
  scroll.addEventListener("scroll", drawTimeline);
  window.addEventListener("resize", () => { if (S.data) { renderOverTime(); renderUnreal(); drawTimeline(); } });
  $("#tl-fit").addEventListener("click", () => { fitTimeline(); drawTimeline(); });
  $("#opt-tl-waiting").addEventListener("change", (e) => { S.tl.waiting = e.target.checked; drawTimeline(); });
  $("#opt-tl-model").addEventListener("change", (e) => { S.tl.model = e.target.checked; drawTimeline(); });
}
function focusTranscript(t) {
  const tr = S.data.transcripts[t];
  const pad = (tr.end - tr.start) * 0.05 + 60;
  S.tl.v0 = tr.start - pad; S.tl.v1 = tr.end + pad; S.tl.highlight = t;
  const r = S.tl.rows.findIndex((row) => row.t === t);
  const card = $("#tl-scroll").closest(".card");
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  if (r >= 0) $("#tl-scroll").scrollTop = Math.max(0, r * ROW_H - 120);
  drawTimeline();
}
function focusInterval(i) {
  const iv = S.data.iv;
  const pad = Math.max(120, (iv.e[i] - iv.s[i]) * 0.3);
  focusTranscript(iv.t[i]);
  S.tl.v0 = iv.s[i] - pad; S.tl.v1 = iv.e[i] + pad;
  drawTimeline();
}

// ---------------------------------------------------------------------------------------------------------- tooltip
const tip = $("#tip");
function showTip(ev, lines) {
  tip.replaceChildren(...lines.filter((l) => l !== "" && l != null).map((l, idx) => {
    if (Array.isArray(l)) return el("div", { class: "row" }, el("span", {}, el("span", { class: "k", style: `background:${l[1]}` }), l[0]));
    if (typeof l === "string") return el("div", { class: idx === 0 ? "" : "m" }, l);
    return el("div", {}, l.k ? el("span", { class: "k", style: `background:${l.k}` }) : null, l.v ? el("span", { class: "v" }, l.v + " ") : null, l.m ? el("span", { class: "m" }, l.m) : null);
  }));
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = `${Math.max(4, x)}px`; tip.style.top = `${Math.max(4, y)}px`;
}
function hideTip() { tip.hidden = true; }

// ---------------------------------------------------------------------------------------------------------- controls
function fillSelect(sel, items, current, allLabel) {
  sel.replaceChildren(el("option", { value: "" }, allLabel), ...items.map(([v, label]) => el("option", { value: v }, label)));
  sel.value = items.some(([v]) => v === current) ? current : "";
  return sel.value;
}
function fillFilters() {
  const D = S.data;
  const mains = D.transcripts.filter((t) => t.role === "main").sort((a, b) => b.end - a.end);
  S.f.session = fillSelect($("#f-session"), mains.map((t) => [String(t.idx), `${t.title}${t.live ? " (live)" : ""}`]), S.f.session, "All sessions");
  const wsBusy = new Map();
  for (const t of D.transcripts) wsBusy.set(t.workstream, (wsBusy.get(t.workstream) || 0) + t.busy);
  S.f.ws = fillSelect($("#f-ws"), D.workstreams.map((w) => [w, w]).sort((a, b) => (wsBusy.get(b[0]) || 0) - (wsBusy.get(a[0]) || 0)), S.f.ws, "All workstreams");
  S.f.branch = fillSelect($("#f-branch"), D.branches.filter(Boolean).map((b) => [b, b]).sort(), S.f.branch, "All branches");
  $("#f-range").value = S.f.range;
  $("#f-role").value = S.f.role;
  $("#f-search").value = S.f.search;
}
function setFilter(key, value) {
  S.f[key] = value;
  if (key === "range" && value !== "custom") { S.f.from = S.f.to = null; }
  $("#f-" + (key === "session" ? "session" : key)) && ($("#f-" + key).value = value);
  saveHash();
  renderAll();
}
function setCustomRange(a, b) {
  S.f.range = "custom"; S.f.from = a; S.f.to = b;
  $("#f-range").value = "custom";
  saveHash();
  renderAll();
}
function saveHash() {
  const p = new URLSearchParams();
  p.set("project", S.project);
  for (const [k, v] of Object.entries(S.f)) if (v !== "" && v != null && !(k === "range" && v === "all")) p.set(k, typeof v === "number" ? v.toFixed(0) : v);
  history.replaceState(null, "", "#" + p.toString());
  const back = new URLSearchParams({ project: S.project });
  if (/^\d+[hd]$/.test(S.f.range || "")) back.set("range", S.f.range);
  const nav = $("#nav-overview");
  if (nav) nav.href = "./#" + back.toString();
}
function loadHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const out = { project: p.get("project") };
  for (const k of Object.keys(S.f)) if (p.has(k)) S.f[k] = k === "from" || k === "to" ? +p.get(k) : p.get(k);
  return out;
}
function setupControls() {
  $("#f-range").addEventListener("change", (e) => {
    if (e.target.value === "custom") { const [a, b] = [S.tl.v0, S.tl.v1]; setCustomRange(a, b); } else setFilter("range", e.target.value);
  });
  $("#f-session").addEventListener("change", (e) => setFilter("session", e.target.value));
  $("#f-role").addEventListener("change", (e) => setFilter("role", e.target.value));
  $("#f-ws").addEventListener("change", (e) => setFilter("ws", e.target.value));
  $("#f-branch").addEventListener("change", (e) => setFilter("branch", e.target.value));
  let timer = null;
  $("#f-search").addEventListener("input", (e) => { clearTimeout(timer); timer = setTimeout(() => setFilter("search", e.target.value), 200); });
  $("#f-reset").addEventListener("click", () => {
    Object.assign(S.f, { range: "all", from: null, to: null, session: "", role: "", ws: "", branch: "", search: "" });
    fillFilters(); saveHash(); renderAll(); fitTimeline(); drawTimeline();
  });
  $("#opt-waiting").addEventListener("change", (e) => { S.includeWaiting = e.target.checked; renderKinds(); renderOverTime(); });
  $("#opt-bucket").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.bucket = b.dataset.v;
    for (const x of $("#opt-bucket").children) x.classList.toggle("on", x === b);
    renderOverTime();
  });
  $("#opt-slow-kind").addEventListener("change", (e) => { S.slowKind = e.target.value; renderSlow(); });
  $("#project").addEventListener("change", (e) => switchProject(e.target.value));
  $("#theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("devtime-theme", document.documentElement.dataset.theme); } catch (e) { /* private mode */ }
    readPalette(); renderAll();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readPalette(); renderAll(); });
}

// ---------------------------------------------------------------------------------------------------------- loading
async function loadProjects() {
  const r = await getJSON("/api/projects");
  const sel = $("#project");
  sel.replaceChildren(el("option", { value: "*" }, "All projects"), ...r.projects.map((p) => el("option", { value: p.name }, `${p.name} · ${fmtHours(p.busy)}`)));
  return r;
}
async function loadData(keepView) {
  $("#main").classList.add("loading");
  try {
    const data = await getJSON(`/api/data?project=${encodeURIComponent(S.project)}`);
    const first = !S.data || S.data.project !== data.project;
    S.data = data; S.version = data.version; S.prep = prepare(data);
    S.ue = prepUE(data);
    assignColors();
    fillFilters();
    F = makeFilter();
    if (first || !keepView) fitTimeline();
    renderAll();
    $("#foot-gen").textContent = ` · data built ${fmtWhen(data.generated)}`;
  } finally {
    $("#main").classList.remove("loading");
  }
}
async function switchProject(p) {
  S.project = p;
  Object.assign(S.f, { session: "", ws: "", branch: "", search: "" });
  saveHash();
  await loadData(false);
}
// Usage alerts (devtime/alerts.py): active first; resolved ones stay listed for a few hours.
let alertsSig = "";
async function loadAlerts() {
  const r = await getJSON("/api/alerts");
  const items = r.alerts || [];
  const sig = items.map((a) => `${a.key}:${a.active}:${a.value}`).join("|");
  if (sig === alertsSig) return;
  alertsSig = sig;
  const card = $("#alerts-card");
  card.hidden = !items.length;
  if (!items.length) return;
  const active = items.filter((a) => a.active).length;
  $("#alerts-sub").textContent = `${active} active · ${items.length - active} resolved (last 6 h)`;
  table($("#alerts"), [
    { key: "active", label: "", render: (a) => el("span", { class: "dot " + (a.active ? "on" : "") }, a.active ? "●" : "○"), sortv: (a) => (a.active ? 1 : 0) },
    { key: "owner", label: "Who", cls: "label", render: (a) => `${a.owner}${a.model ? ` (${a.model})` : ""}`, title: (a) => a.agent || a.session || "" },
    { key: "project", label: "Project", render: (a) => a.project || "" },
    { key: "kind", label: "Alert", cls: "nowrap" },
    { key: "text", label: "What (hover: suggested fix)", cls: "label", title: (a) => a.advice || "" },
    { key: "first", label: "Since", num: true, cls: "nowrap", render: (a) => fmtWhen(a.first) },
    { key: "last", label: "Last seen", num: true, render: (a) => fmtTime(a.last) },
  ], items, { sort: "last", limit: 100 });
}

let lastCheck = 0;
async function poll() {
  try {
    const v = await getJSON("/api/version");
    lastCheck = Date.now();
    if (v.version !== S.version) {
      await loadProjects().then(() => { $("#project").value = S.project; });
      await loadData(true);
    }
    updateLive(v);
    loadAlerts().catch(() => {});
  } catch (e) {
    $("#live").textContent = "server unreachable";
    $("#live").classList.remove("on");
  }
}
function updateLive(v) {
  const live = S.data ? S.data.transcripts.filter((t) => t.live).length : 0;
  const ago = v && v.generated ? Math.round(Date.now() / 1000 - v.generated) : null;
  $("#live").textContent = `${live ? `${live} transcript${live === 1 ? "" : "s"} live` : "idle"} · data ${ago != null ? fmtDur(ago) : "?"} old`;
  $("#live").classList.toggle("on", live > 0);
}

async function init() {
  try { const th = localStorage.getItem("devtime-theme"); if (th) document.documentElement.dataset.theme = th; } catch (e) { /* ignore */ }
  readPalette();
  setupControls();
  setupTimeline();
  setupUnreal();
  const h = loadHash();
  const r = await loadProjects();
  const names = r.projects.map((p) => p.name);
  S.project = h.project && (names.includes(h.project) || h.project === "*") ? h.project : (names.includes(r.defaultProject) ? r.defaultProject : names[0]);
  $("#project").value = S.project;
  await loadData(false);
  updateLive({ generated: S.data.generated });
  loadAlerts().catch(() => {});
  setInterval(poll, 5000);
}
init().catch((e) => { document.body.prepend(el("pre", { style: "color:#d03b3b;padding:16px" }, String(e.stack || e))); });
