"use strict";
// Overview: performance and efficiency at a glance, from /api/overview (computed server-side). Details: details.html.

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v != null) n.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return n;
};
const svg = (tag, attrs = {}) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
const pad2 = (n) => String(n).padStart(2, "0");
const money = (x) => (x == null ? "–" : x >= 1000 ? `$${(x / 1000).toFixed(x >= 10000 ? 0 : 1)}k` : x >= 100 ? `$${Math.round(x)}` : `$${x.toFixed(x >= 10 ? 1 : 2)}`);
// Costs are shown as a share of the weekly limit ("% of week") once the plan's readings give an allowance (API-equivalent
// dollars per 100 % of the week); the dollar amount stays in the tooltip. Without an allowance: dollars.
const hasAllowance = () => S.allowance > 0;
const weekPct = (x) => { const v = 100 * x / S.allowance; return `${v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : v.toFixed(2)} %`; };
const cost = (x) => (x == null ? "–" : hasAllowance() ? weekPct(x) : money(x));
const costTip = (x) => (x != null && hasAllowance() ? `${money(x)} API-equivalent` : undefined);
const costBoth = (x) => (hasAllowance() ? `${weekPct(x)} (${money(x)})` : money(x));
const costCell = (x, cls) => el("td", { class: cls, title: costTip(x) }, cost(x));
const pct = (x) => (x == null ? "–" : `${(100 * x).toFixed(x >= 0.1 || x === 0 ? 0 : 1)} %`);
const dur = (s) => {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${pad2(Math.floor((s % 3600) / 60))}`;
};
const tok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`);
const clock = (t) => { const d = new Date(t * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const when = (t) => { const d = new Date(t * 1000); return `${DOW[d.getDay()]} ${clock(t)}`; };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

const S = { project: "*", range: "24h", data: null, alerts: [], allowance: null };

// ------------------------------------------------------------------------------------------------------------ tiles
// delta: change against the previous period of the same length; better: "down" or "up" is the good direction.
function tile(label, value, detail, help, cur, prev, better, extra) {
  const t = el("div", { class: "tile", title: help });
  const v = el("div", { class: "value" }, value);
  if (cur != null && prev != null && prev > 0 && better) {
    const ch = (cur - prev) / prev;
    if (Math.abs(ch) >= 0.05) {
      const good = (ch < 0) === (better === "down");
      v.append(el("span", { class: `delta ${good ? "good" : "bad"}`, title: "against the previous period of the same length" },
        `${ch > 0 ? "▲" : "▼"} ${Math.abs(100 * ch).toFixed(0)} %`));
    } else v.append(el("span", { class: "delta flat" }, "≈"));
  }
  t.append(el("div", { class: "label" }, label), v, el("div", { class: "detail" }, detail));
  if (extra) t.append(extra);
  return t;
}

// "$2,600 ± 200": the fitted allowance and its uncertainty (one standard error), when the fit has one.
function allowanceText(L, fallback) {
  const a = L && L.allowance > 0 ? L.allowance : fallback;
  return L && L.allowanceError > 0 && L.allowance > 0 ? `${money(a)} ± ${money(L.allowanceError)}` : money(a);
}

// A reading of the plan's usage from the app in the last hour: the weekly numbers are then the app's own.
const fresh = (L) => L && L.reading && L.reading.exact && L.now - L.reading.t < 3600;

function renderTiles(o) {
  const c = o.current, p = o.previous || {};
  const share = (a, b) => (b ? a / b : null);
  const wk = o.week;
  const gauge = wk.share != null ? el("div", { class: `gauge ${wk.share > 1 ? "over" : wk.share > 0.8 ? "warn" : ""}` },
    el("i", { style: `width:${Math.min(100, 100 * wk.share).toFixed(1)}%` })) : null;
  if (gauge) gauge.firstChild.style.width = `${Math.min(100, 100 * wk.share).toFixed(1)}%`;
  $("#eff").replaceChildren(
    tile(hasAllowance() ? "Cost (% of week)" : "Cost", cost(c.cost), `${cost(c.costPerHour)}/h · subagents ${pct(share(c.subCost, c.cost))} · Opus ${pct(share(c.opusCost, c.cost))}`,
      `Estimated API-equivalent cost of every request in the range (token counts × config pricing)${hasAllowance() ? `: ${money(c.cost)}, ${money(c.costPerHour)}/h. Shown as a share of the weekly limit (allowance ~${money(S.allowance)}).` : "."}`,
      c.costPerHour, p.costPerHour, "down"),
    tile(hasAllowance() ? "Per commit (% of week)" : "Cost per commit", cost(c.costPerCommit), `${c.commits} commits (from git calls, approx.)`,
      `Cost divided by the commits agents made in the range: a rough cost of one unit of finished work${hasAllowance() && c.costPerCommit != null ? ` (${money(c.costPerCommit)})` : ""}.`,
      c.costPerCommit, p.costPerCommit, "down"),
    tile("Avoidable: cache churn", cost(c.churn), `${pct(share(c.churn, c.cost))} of cost · subagents re-writing their cache after waits`,
      "Requests in which a subagent re-wrote its whole prompt cache after waiting over 5 minutes (its cache lives 5 minutes). Long jobs belong to the main session.",
      share(c.churn, c.cost), share(p.churn, p.cost), "down"),
    tile("Big-context share", pct(share(c.big, c.cost)), `${cost(c.big)} in requests over 300k tokens of context`,
      "Share of cost in requests carrying over 300k tokens of context; every call re-reads all of it. Hand off to a fresh agent earlier.",
      share(c.big, c.cost), share(p.big, p.cost), "down"),
    tile("Cache hit", pct(c.cacheHit), `${c.requests.toLocaleString()} requests`,
      "Share of input tokens read from the prompt cache (cheap) rather than written or sent fresh.",
      c.cacheHit, p.cacheHit, "up"),
    tile(o.limits ? (fresh(o.limits) ? "Weekly limit used" : "Weekly limit used (est.)") : "7-day usage (est.)", wk.share != null ? pct(wk.share) : money(wk.cost),
      wk.budget ? `${money(wk.cost)} of ~${allowanceText(o.limits, wk.budget)}${o.limits ? ` · resets ${when(o.limits.end)}` : " weekly allowance"}, all projects` : "set weekly_budget_usd in config",
      o.limits ? "Estimated cost since the plan's last weekly reset (weekly_reset in config/devtime.toml) against weekly_budget_usd, calibrated from the usage % the plan reported."
        : "Last 7 days' estimated cost against weekly_budget_usd (config/devtime.toml). Set weekly_reset to follow the plan's own week.",
      null, null, null, gauge),
  );
  const kinds = Object.entries(c.kindTime || {});
  const agentTot = kinds.reduce((a, [, v]) => a + v, 0) || 1;
  const topKinds = kinds.slice(0, 3).map(([k, v]) => `${k} ${pct(v / agentTot)}`).join(" · ");
  const range = c.hi - c.lo;
  $("#perf").replaceChildren(
    tile("Commits / hour", c.commitsPerHour.toFixed(c.commitsPerHour >= 10 ? 0 : 1), `${c.commits} in ${dur(range)}`,
      "Commits agents made per hour of the range: a throughput proxy.", c.commitsPerHour, p.commitsPerHour, "up"),
    tile("Working", pct(c.busyShare), `${c.parallel.toFixed(1)}× agents in parallel while working`,
      "Share of the range when at least one session or agent was doing something (not waiting or idle), and how many at once on average.",
      c.busyShare, p.busyShare, "up"),
    tile("Waiting on you", dur(c.blockedOnUser), `${pct(c.blockedOnUser / Math.max(range, 1))} of the range, nothing else running`,
      "Wall-clock time when a session was waiting for your reply and no agent was working.",
      c.blockedOnUser, p.blockedOnUser, "down"),
    tile("Agent time", `${(c.agentTime / 3600).toFixed(c.agentTime >= 36000 ? 0 : 1)} h`, topKinds,
      "Time spent by all sessions and agents together (overlapping work split), by what it was spent on.",
      null, null, null),
    tile("Unreal editor", dur(c.editor), `avoidable ${dur(c.editorAvoidable)} (throttling, fixed waits, timeouts, slow startups)`,
      "Editor run time in the range, and how much of it the log findings flag as plain waste. Python stalls and slow tests are listed on the details page.",
      share(c.editorAvoidable, c.editor), share(p.editorAvoidable, p.editor), "down"),
  );
}

// ------------------------------------------------------------------------------------------------------------- live
function renderLive(o) {
  const rows = o.live;
  const alertsBy = new Map();
  for (const a of S.alerts) if (a.active) {
    const k = a.agent || a.session;
    if (!alertsBy.has(k)) alertsBy.set(k, []);
    alertsBy.get(k).push(a.kind);
  }
  const subs = rows.filter((r) => r.role !== "main").length;
  const mains = rows.filter((r) => r.role === "main").length;
  $("#live-sub").textContent = rows.length ? `${mains} session${mains === 1 ? "" : "s"} · ${subs} subagent${subs === 1 ? "" : "s"} · active in the last 10 min` : "";
  if (!rows.length) { $("#live-list").replaceChildren(el("div", { class: "sub" }, "Nothing running.")); return; }
  const now = o.now;
  const head = el("tr", {}, ...["", "Model", "Doing now", "Context", hasAllowance() ? "Last 30 min (% of week)" : "Last 30 min", hasAllowance() ? "Total (% of week)" : "Total"].map((h, i) => el("th", { class: i >= 3 ? "num" : "" }, h)));
  const body = rows.map((r) => {
    const main = r.role === "main";
    const doing = r.now ? `${r.now.kind}${r.now.label ? ` · ${r.now.label}` : ""}` : "";
    const forS = r.now ? now - r.now.since : 0;
    const name = el("td", { class: main ? "group" : "indent label", title: r.name }, r.name);
    for (const k of alertsBy.get(r.id) || []) name.append(el("span", { class: "badge", title: "active usage alert" }, k));
    return el("tr", {},
      name,
      el("td", {}, r.model),
      el("td", { class: `label doing ${r.now && r.now.running ? "running" : ""}`, title: doing },
        !doing ? "" : r.now.running ? `${doing}${forS > 60 ? ` (for ${dur(forS)})` : ""}` : `last: ${doing}, ${dur(now - r.end)} ago`),
      el("td", { class: "num" }, r.context ? tok(r.context) : ""),
      costCell(r.burn30, "num"),
      costCell(r.cost, "num"));
  });
  $("#live-list").replaceChildren(el("table", { class: "live-table" }, el("thead", {}, head), el("tbody", {}, ...body)));
}

// ----------------------------------------------------------------------------------------------------------- alerts
function renderAlerts() {
  const items = S.alerts;
  const card = $("#alerts-card");
  card.hidden = !items.length;
  if (!items.length) return;
  const active = items.filter((a) => a.active).length;
  $("#alerts-sub").textContent = `${active} active · ${items.length - active} resolved (last 6 h) · each passed once to the agent and its main session`;
  const head = el("tr", {}, ...["", "Who", "Alert", "What (hover: suggested fix)", "Since"].map((h, i) => el("th", { class: i === 4 ? "num" : "" }, h)));
  const body = items.slice(0, 30).map((a) => el("tr", {},
    el("td", {}, el("span", { class: `dot ${a.active ? "on" : ""}` }, a.active ? "●" : "○")),
    el("td", { class: "label", title: a.agent || a.session || "" }, `${a.owner}${a.model ? ` (${a.model})` : ""}`),
    el("td", { class: "nowrap" }, a.kind),
    el("td", { class: "label", title: a.advice || "" }, a.text),
    el("td", { class: "num nowrap" }, when(a.first))));
  $("#alerts").replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}

// ------------------------------------------------------------------------------------------------------------ trend
function renderTrend(o) {
  const t = o.trend, n = t.main.length;
  const host = $("#trend");
  const W = Math.max(300, host.clientWidth || 500), H = 200, m = { l: 44, r: 6, t: 8, b: 24 };
  const u = hasAllowance() ? 100 / S.allowance : 1;  // chart units: % of the week, else dollars
  const series = [["main sessions", t.main.map((v) => v * u), css("--s1")],
    ["subagents", t.sub.map((v, i) => Math.max(0, v - t.churn[i]) * u), css("--s3")],
    ["avoidable (cache churn)", t.churn.map((v) => v * u), css("--critical")]];
  const tot = t.main.map((v, i) => (v + t.sub[i]) * u);
  const maxV = Math.max(hasAllowance() ? 0.1 : 1, ...tot);
  const stepV = (hasAllowance() ? [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100] : [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000]).find((s) => s * 4 >= maxV) || 1000;
  const yMax = stepV * 4;
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Cost per bucket" });
  const g = svg("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const y = m.t + (H - m.t - m.b) * (1 - i / 4);
    g.append(svg("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }));
    const tx = svg("text", { x: m.l - 6, y: y + 4, "text-anchor": "end" }); tx.textContent = hasAllowance() ? `${+(stepV * i).toFixed(2)}%` : `$${stepV * i}`; g.append(tx);
  }
  root.append(g);
  const bw = (W - m.l - m.r) / n, barW = Math.max(1, Math.min(22, bw - 2));
  const every = Math.max(1, Math.ceil(56 / bw));
  for (let i = 0; i < n; i++) {
    const x = m.l + i * bw + (bw - barW) / 2;
    let y = H - m.b;
    for (const [, vals, color] of series) {
      const h = (H - m.t - m.b) * vals[i] / yMax;
      if (h <= 0) continue;
      root.append(svg("rect", { x, y: y - h, width: barW, height: Math.max(0.5, h), fill: color }));
      y -= h;
    }
    const k = t.start + i * t.step;
    const hit = svg("rect", { x: m.l + i * bw, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent" });
    const tipText = `${when(k)}–${clock(k + t.step)}: ${costBoth(t.main[i] + t.sub[i])} (main ${costBoth(t.main[i])}, subagents ${costBoth(t.sub[i])}, avoidable ${costBoth(t.churn[i])})`;
    hit.append(svg("title", {})); hit.firstChild.textContent = tipText;
    root.append(hit);
    if (i % every === 0) {
      const d = new Date(k * 1000);
      const tx = svg("text", { x: m.l + i * bw + bw / 2, y: H - 7, "text-anchor": "middle", fill: css("--text-muted"), "font-size": 11 });
      tx.textContent = t.step >= 86400 ? `${d.getDate()}/${d.getMonth() + 1}` : t.step >= 3600 && d.getHours() === 0 ? DOW[d.getDay()] : clock(k);
      root.append(tx);
    }
  }
  host.replaceChildren(root);
  $("#trend-sub").textContent = `per ${t.step >= 86400 ? "day" : t.step >= 3600 ? "hour" : `${t.step / 60} min`}${hasAllowance() ? " · % of the weekly limit" : ""}`;
  $("#trend-legend").replaceChildren(...series.map(([name, , color]) => el("span", {}, el("i", { style: `background:${color}` }), name)));
  for (const i of $("#trend-legend").querySelectorAll("i")) i.style.background = series.find((s) => s[0] === i.parentNode.textContent)[2];
}

// ----------------------------------------------------------------------------------------------------- weekly limit
// Usage since the last weekly reset as a share of the weekly allowance, the limit, and where the week ends at the
// pace since the weekly reset and since the session (5-hour window) reset. Always the whole account's week.
function renderLimit(o) {
  const L = o.limits, host = $("#limit");
  if (!L) {
    host.replaceChildren(el("div", { class: "sub" }, "No usage reading for this organisation yet. Run /devtime:start in a Claude Code session: it reads the plan's usage every 30 minutes. (Or set weekly_reset in devtime.toml.)"));
    $("#limit-legend").replaceChildren();
    return;
  }
  const W = Math.max(300, host.clientWidth || 500), m = { l: 44, r: 10, t: 10, b: 24 };
  const Xw = (t) => m.l + (W - m.l - m.r) * (t - L.start) / (L.end - L.start);
  const lanes = [];  // large pieces of work, packed into rows under the axis
  for (const w of L.work || []) {
    const x0 = Xw(w.from), x1 = Math.max(Xw(w.to), x0 + 3), labelEnd = x0 + 4 + w.name.length * 6;
    let row = lanes.findIndex((end) => x0 > end + 6);
    if (row < 0) { row = lanes.length; lanes.push(0); }
    lanes[row] = Math.max(x1, labelEnd);
    w.row = row; w.x0 = x0; w.x1 = x1;
  }
  const laneH = 15, plotH = 236, H = plotH + (lanes.length ? 8 + lanes.length * laneH : 0);
  m.b = H - plotH + 24;
  const cUsed = css("--s1"), cPace = [css("--s2"), css("--s7")];
  const yMax = Math.max(1.06, L.share * 1.04);  // up to the limit: past it doesn't matter
  const X = (t) => m.l + (W - m.l - m.r) * (t - L.start) / (L.end - L.start);
  const Y = (v) => m.t + (H - m.t - m.b) * (1 - Math.min(v, yMax) / yMax);
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Weekly limit used" });
  for (const s of L.sessions) {  // 5-hour session windows: the current one shaded, earlier ones as ticks
    const x0 = X(Math.max(s, L.start)), x1 = X(Math.min(s + 5 * 3600, L.end));
    const r = L.session.start === s
      ? svg("rect", { x: x0, y: m.t, width: Math.max(1, x1 - x0), height: H - m.t - m.b, fill: cUsed, opacity: 0.15 })
      : svg("rect", { x: x0, y: H - m.b - 5, width: Math.max(1, x1 - x0 - 1.5), height: 5, fill: cUsed, opacity: 0.35 });
    r.append(svg("title", {})); r.firstChild.textContent = `5-hour session window ${when(s)}–${clock(s + 5 * 3600)}`;
    root.append(r);
  }
  const g = svg("g", { class: "axis" });
  const vStep = 0.25;
  for (let v = 0; v <= Math.min(yMax, 1) + 1e-9; v += vStep) {
    g.append(svg("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const tx = svg("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }); tx.textContent = `${Math.round(v * 100)}%`; g.append(tx);
  }
  root.append(g);
  const day = new Date(L.start * 1000);  // day labels at each local midnight
  day.setHours(24, 0, 0, 0);
  for (; day.getTime() / 1000 < L.end; day.setDate(day.getDate() + 1)) {
    const t = day.getTime() / 1000;
    if (X(t) + 40 > W - m.r) break;  // no room for the label
    root.append(svg("line", { x1: X(t), x2: X(t), y1: H - m.b, y2: H - m.b + 4, stroke: css("--text-muted") }));
    const tx = svg("text", { x: X(t) + 3, y: H - m.b + 15, fill: css("--text-muted"), "font-size": 11 });
    tx.textContent = `${DOW[day.getDay()]} ${day.getDate()}`;
    root.append(tx);
  }
  const lim = svg("line", { x1: m.l, x2: W - m.r, y1: Y(1), y2: Y(1), stroke: css("--critical"), "stroke-width": 1.5 });
  lim.append(svg("title", {})); lim.firstChild.textContent = `weekly limit (~${allowanceText(L, L.budget)} API-equivalent)`; root.append(lim);
  const lt = svg("text", { x: m.l + 4, y: Y(1) - 4, fill: css("--critical"), "font-size": 11 }); lt.textContent = "weekly limit"; root.append(lt);
  const pts = L.used.map((v, i) => [X(Math.min(L.start + (i + 1) * L.step, L.now)), Y(v)]);
  pts.unshift([X(L.start), Y(0)]);
  const path = pts.map((p) => p.join(",")).join("L");
  root.append(svg("path", { d: `M${path}L${pts[pts.length - 1][0]},${Y(0)}Z`, fill: cUsed, opacity: 0.2 }));
  root.append(svg("path", { d: `M${path}`, fill: "none", stroke: cUsed, "stroke-width": 2 }));
  L.pace.forEach((p, i) => {
    const endV = p.atReset;
    const hit = endV > 1 && L.share < 1;  // stop at the limit
    const tEnd = hit ? L.now + (L.end - L.now) * (1 - L.share) / (endV - L.share) : L.end;
    const line = svg("line", { x1: X(L.now), y1: Y(L.share), x2: X(tEnd), y2: Y(hit ? 1 : endV), stroke: cPace[i], "stroke-width": 2, "stroke-dasharray": "5 3" });
    line.append(svg("title", {})); line.firstChild.textContent = `pace ${p.label} (${when(p.from)}): ${pct(p.perHour)} of the week per hour → ${pct(endV)} at reset`;
    root.append(line);
    if (p.limitAt) {
      const c = svg("circle", { cx: X(p.limitAt), cy: Y(1), r: 4, fill: cPace[i] });
      c.append(svg("title", {})); c.firstChild.textContent = `limit reached ${when(p.limitAt)} at this pace`;
      root.append(c);
    }
  });
  root.append(svg("line", { x1: X(L.now), x2: X(L.now), y1: m.t, y2: H - m.b, stroke: css("--text-muted"), "stroke-dasharray": "2 3" }));
  for (const w of L.work || []) {  // the lane under the axis
    const y = H - m.b + 22 + w.row * laneH;
    const g3 = svg("g", {});
    g3.append(svg("rect", { x: w.x0, y, width: w.x1 - w.x0, height: 10, rx: 2, fill: css("--s5"), opacity: 0.55 }));
    if (!w.ongoing) g3.append(svg("rect", { x: w.x0, y: y - 1, width: 2, height: 12, fill: css("--s5") }));  // where it starts
    const tx = svg("text", { x: w.x0 + 4, y: y + 8.5, "font-size": 10, fill: css("--text-primary") });
    tx.textContent = w.name; g3.append(tx);
    g3.append(svg("title", {}));
    g3.lastChild.textContent = `${w.name}: ${pct(w.share)} of the week (${money(w.cost)}), ${w.ongoing ? "running since before this week" : `from ${when(w.from)}`} to ${when(w.to)}${w.agents ? `, ${w.agents} agents` : ""}`;
    root.append(g3);
  }
  let lastX = -99, row = 0;
  (L.changes || []).forEach((c, i) => {  // where usage changes were made
    const x = X(c.t);
    row = x - lastX < 14 ? row + 1 : 0;
    lastX = x;
    const g2 = svg("g", {});
    g2.append(svg("line", { x1: x, x2: x, y1: m.t, y2: H - m.b - 20, stroke: css("--s3"), "stroke-width": 1, "stroke-dasharray": "1 2" }));
    g2.append(svg("circle", { cx: x, cy: H - m.b - 14 - row * 13, r: 6, fill: css("--s3") }));
    const tx = svg("text", { x, y: H - m.b - 10.5 - row * 13, "text-anchor": "middle", "font-size": 9, fill: "#fff", "font-weight": 600 });
    tx.textContent = i + 1; g2.append(tx);
    g2.append(svg("title", {})); g2.lastChild.textContent = `${i + 1}. ${when(c.t)}: ${c.label}`;
    root.append(g2);
  });
  host.replaceChildren(root);
  $("#limit-changes").replaceChildren(...(L.changes || []).map((c, i) => el("li", {}, el("b", {}, `${i + 1}`), ` ${when(c.t)} · ${c.label}`)));
  const s = L.session;
  const R = L.reading;
  const read = R ? `${pct(R.share)} at ${clock(R.t)} (app${R.exact ? "" : " history"})` + (L.now - R.t > 600 ? ` · ~${pct(L.share)} now` : "") : `~${pct(L.share)} (est.)`;
  $("#limit-sub").textContent = `${L.allowance > 0 ? `allowance ~${allowanceText(L)} · ` : ""}${read} · resets ${L.resetGuessed ? "≈ " : ""}${when(L.end)}` + (s.start ? ` · 5-hour window ${R && R.exact && L.now - R.t < 3600 ? `${pct(R.five)} used, ` : ""}ends ${clock(s.end)}` : "");
  const fate = (p) => (p.limitAt ? `limit hit ${when(p.limitAt)}` : `${pct(p.atReset)} at reset`);
  const items = [[R ? "used" : "used (est.)", cUsed, L.fit ? `fitted to ${L.fit.samples} app samples: allowance ~${allowanceText(L)} API-equivalent${L.fit.sigma != null ? `; readings scatter ±${(100 * L.fit.sigma).toFixed(1)} points around the fit` : ""}${familyText(L.fit)}` : `weekly_budget_usd ${money(L.budget)}`],
    ...L.pace.map((p, i) => [`pace ${p.label}: ${fate(p)}`, cPace[i], `from ${when(p.from)}`]),
    ["5-hour session windows", cUsed, ""]];
  $("#limit-legend").replaceChildren(...items.map(([name, color, title], i) => {
    const sw = el("i", {}); sw.style.background = color; if (i === items.length - 1) sw.style.opacity = 0.3;
    return el("span", title ? { title } : {}, sw, name);
  }));
}

// What each model family seems to weigh against the plan (reported by the fit when the data separates them).
function familyText(fit) {
  const f = fit && fit.byFamily;
  if (!f) return "";
  return "; per model: " + Object.entries(f).map(([k, v]) => `${k} ~${money(v.allowance)} ± ${money(v.error)} (${pct(v.share)} of cost)`).join(", ");
}

// ------------------------------------------------------------------------------------------------------- throughput
function renderThroughput(o) {
  const t = o.trend, n = t.commits.length, host = $("#through");
  const W = Math.max(300, host.clientWidth || 500), H = 220, m = { l: 34, r: 6, t: 10, b: 24 };
  const maxV = Math.max(1, ...t.commits, ...t.agents);
  const stepV = [1, 2, 3, 5, 10, 20, 25, 50, 100, 200].find((s) => s * 4 >= maxV) || 500, yMax = stepV * 4;
  const Y = (v) => m.t + (H - m.t - m.b) * (1 - v / yMax);
  const root = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Commits and finished subagents per bucket" });
  const g = svg("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    g.append(svg("line", { x1: m.l, x2: W - m.r, y1: Y(stepV * i), y2: Y(stepV * i) }));
    const tx = svg("text", { x: m.l - 6, y: Y(stepV * i) + 4, "text-anchor": "end" }); tx.textContent = stepV * i; g.append(tx);
  }
  root.append(g);
  const bw = (W - m.l - m.r) / n, barW = Math.max(1, Math.min(22, bw - 2)), every = Math.max(1, Math.ceil(56 / bw));
  const c1 = css("--s3"), c2 = css("--s2");
  const line = [];
  for (let i = 0; i < n; i++) {
    if (t.commits[i]) root.append(svg("rect", { x: m.l + i * bw + (bw - barW) / 2, y: Y(t.commits[i]), width: barW, height: H - m.b - Y(t.commits[i]), fill: c1 }));
    line.push(`${m.l + i * bw + bw / 2},${Y(t.agents[i])}`);
    const k = t.start + i * t.step;
    const hit = svg("rect", { x: m.l + i * bw, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent" });
    hit.append(svg("title", {})); hit.firstChild.textContent = `${when(k)}–${clock(k + t.step)}: ${t.commits[i]} commits, ${t.agents[i]} subagents finished`;
    root.append(hit);
    if (i % every === 0) {
      const d = new Date(k * 1000);
      const tx = svg("text", { x: m.l + i * bw + bw / 2, y: H - 7, "text-anchor": "middle", fill: css("--text-muted"), "font-size": 11 });
      tx.textContent = t.step >= 86400 ? `${d.getDate()}/${d.getMonth() + 1}` : t.step >= 3600 && d.getHours() === 0 ? DOW[d.getDay()] : clock(k);
      root.append(tx);
    }
  }
  root.append(svg("polyline", { points: line.join(" "), fill: "none", stroke: c2, "stroke-width": 1.5, "pointer-events": "none" }));
  host.replaceChildren(root);
  const unit = t.step >= 86400 ? "day" : t.step >= 3600 ? "hour" : `${t.step / 60} min`;
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  $("#through-sub").textContent = `per ${unit} · ${sum(t.commits)} commits, ${sum(t.agents)} subagents finished`;
  $("#through-legend").replaceChildren(...[["commits", c1], ["subagents finished", c2]].map(([name, color]) => {
    const sw = el("i", {}); sw.style.background = color; return el("span", {}, sw, name);
  }));
}

// --------------------------------------------------------------------------------------------------------- spenders
function renderSpenders(o) {
  const rows = o.spenders;
  if (!rows.length) { $("#spenders").replaceChildren(el("div", { class: "sub" }, "No requests in this range.")); return; }
  const head = el("tr", {}, ...["Who", "Model", hasAllowance() ? "Cost (% of week)" : "Cost", "Avoidable", "Context (median)", "Requests"].map((h, i) => el("th", { class: i >= 2 ? "num" : "" }, h)));
  const body = rows.map((r) => el("tr", {},
    el("td", { class: "label", title: r.session ? `subagent of ${r.session}` : "main session" }, r.role === "main" ? `${r.name} (session)` : r.name),
    el("td", {}, r.model),
    costCell(r.cost, "num"),
    r.churn ? costCell(r.churn, "num") : el("td", { class: "num" }),
    el("td", { class: "num" }, tok(r.context)),
    el("td", { class: "num" }, r.requests.toLocaleString())));
  $("#spenders").replaceChildren(el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}

// ---------------------------------------------------------------------------------------------------------- monitoring
function renderMonitoring(o) {
  const m = o.monitoring || { total: { wakes: 0, requests: 0, cost: 0 }, byTag: [], bySession: [] };
  const t = m.total;
  $("#mon-sub").textContent = t.requests ? `${t.wakes.toLocaleString()} wakes · ${cost(t.cost)}${t.share != null ? ` · ${pct(t.share)} of cost in this range` : ""}` : "";
  const cols = [["Wakes", (r) => r.wakes.toLocaleString(), 1], ["Avg cost / wake", (r) => r.avgPerWake == null ? "" : cost(r.avgPerWake), 1],
    ["Cost", (r) => cost(r.cost), 1], ["Input", (r) => tok(r.in), 1], ["Cache read", (r) => tok(r.cacheRead), 1],
    ["Cache write", (r) => tok(r.cacheWrite), 1], ["Output", (r) => tok(r.out), 1]];
  smallTable($("#mon-tags"), "By monitor", [["Monitor", (r) => r.tag || "all"], ...cols],
    m.byTag.length > 1 ? [...m.byTag, Object.assign({ tag: "total" }, t)] : m.byTag, "No Monitor wakes in this range.");
  smallTable($("#mon-sessions"), "By session", [["Session", (r) => r.session,],
    ["Monitors", (r) => Object.entries(r.tags).map(([k, v]) => `${k} ${v.wakes}`).join(", ")], ...cols.slice(0, 3)],
    m.bySession, "No Monitor wakes in this range.");
}

// ------------------------------------------------------------------------------------------------------ experiments
function smallTable(host, title, cols, rows, empty) {
  if (!rows.length) { host.replaceChildren(el("h3", {}, title), el("div", { class: "sub" }, empty)); return; }
  const head = el("tr", {}, ...cols.map(([label, , num]) => el("th", { class: num ? "num" : "" }, label)));
  const body = rows.map((r) => el("tr", {}, ...cols.map(([, f, num]) => el("td", { class: num ? "num" : "" }, f(r)))));
  host.replaceChildren(el("h3", {}, title), el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}
function renderExperiments(o) {
  const x = o.experiments || { ab: [], runner: [] };
  $("#exp-sub").textContent = `last ${x.days} days, all of this project's sessions`;
  const named = x.ab.some((r) => r.experiment);
  smallTable($("#exp-ab"), "Implementation: Opus vs Sonnet vs Opus plan → Sonnet", [
    ...(named ? [["Experiment", (r) => r.experiment || "legacy"]] : []), ["Arm", (r) => r.model], ["Stages", (r) => r.stages, 1], ["Cost / stage (median)", (r) => cost(r.medianCost), 1],
    ["Time / stage", (r) => dur(r.medianMinutes * 60), 1], ["Needed fix-ups", (r) => pct(r.rework), 1],
    ["Agents / stage", (r) => r.agentsPerStage.toFixed(1), 1], ["Cost / commit", (r) => cost(r.costPerCommit), 1],
  ], x.ab, "No AB-tagged stages yet.");
  smallTable($("#exp-runner"), "Runner: Haiku vs Sonnet", [
    ["Model", (r) => r.model], ["Jobs", (r) => r.runs, 1], ["Cost / job (median)", (r) => cost(r.medianCost), 1],
    ["Time / job", (r) => dur(r.medianMinutes * 60), 1], ["Context", (r) => tok(r.medianContext), 1],
  ], x.runner, "No runner jobs yet.");
  smallTable($("#exp-types"), "Fixed context by agent type (last 7 days)", [
    ["Agent type", (r) => r.type], ["Agents", (r) => r.agents, 1], ["Fixed context", (r) => tok(r.baseContext), 1],
    ["Requests", (r) => r.requests.toLocaleString(), 1], ["Cost / agent (median)", (r) => cost(r.medianCost), 1],
  ], x.types || [], "No subagents in the last 7 days.");
}

// ------------------------------------------------------------------------------------------------------------- load
async function getJSON(url) {
  const r = await fetch(withOrg(url));
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
function saveHash() {
  const p = new URLSearchParams({ project: S.project, range: S.range });
  history.replaceState(null, "", "#" + p.toString());
  $("#details").href = "details.html#" + p.toString();
}
async function refresh() {
  try {
    const [o, a] = await Promise.all([
      getJSON(`/api/overview?project=${encodeURIComponent(S.project)}&range=${S.range}`),
      getJSON("/api/alerts").catch(() => ({ alerts: [] })),
    ]);
    S.data = o;
    S.allowance = o.limits && o.limits.allowance > 0 ? o.limits.allowance : null;
    S.alerts = (a.alerts || []).filter((x) => S.project === "*" || !x.project || x.project === S.project);
    renderTiles(o); renderLive(o); renderAlerts(); renderTrend(o); renderLimit(o); renderThroughput(o); renderSpenders(o); renderMonitoring(o); renderExperiments(o);
    $("#live").textContent = `updated ${clock(o.now)}`;
    $("#live").classList.toggle("on", o.live.length > 0);
  } catch (e) {
    $("#live").textContent = "server unreachable";
    $("#live").classList.remove("on");
  }
}
async function init() {
  try { const th = localStorage.getItem("devtime-theme"); if (th) document.documentElement.dataset.theme = th; } catch (e) { /* private mode */ }
  await initOrgs();
  const h = new URLSearchParams(location.hash.slice(1));
  const r = await getJSON("/api/projects");
  const names = r.projects.map((p) => p.name);
  S.project = names.includes(h.get("project")) || h.get("project") === "*" ? h.get("project") : (r.defaultProject && names.includes(r.defaultProject) ? r.defaultProject : "*");
  if (h.get("range") && $(`#range button[data-v="${h.get("range")}"]`)) S.range = h.get("range");
  const sel = $("#project");
  sel.replaceChildren(el("option", { value: "*" }, "All projects"), ...names.map((n) => el("option", { value: n }, n)));
  sel.value = S.project;
  sel.addEventListener("change", () => { S.project = sel.value; saveHash(); refresh(); });
  for (const b of $("#range").children) b.classList.toggle("on", b.dataset.v === S.range);
  $("#range").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.range = b.dataset.v;
    for (const x of $("#range").children) x.classList.toggle("on", x === b);
    saveHash(); refresh();
  });
  $("#theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("devtime-theme", document.documentElement.dataset.theme); } catch (e) { /* private mode */ }
    if (S.data) { renderTrend(S.data); renderLimit(S.data); renderThroughput(S.data); }
  });
  window.addEventListener("resize", () => { if (S.data) { renderTrend(S.data); renderLimit(S.data); renderThroughput(S.data); } });
  saveHash();
  await refresh();
  setInterval(refresh, 15000);
}
init().catch((e) => { document.body.prepend(el("pre", { style: "color:#d03b3b;padding:16px" }, String(e.stack || e))); });
