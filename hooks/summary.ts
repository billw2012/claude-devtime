import type { Live, Summary } from '../types'

// The devtime server's /api/overview and /api/alerts, reduced to what the pane shows.

type Window = { costPerHour?: number; commits?: number; costPerCommit?: number | null; commitsPerHour?: number;
  cacheHit?: number | null; churn?: number; big?: number; cost?: number }
type Pace = { perHour?: number; atReset?: number; limitAt?: number | null }
type Alert = { key?: string; active?: boolean; title?: string; text?: string }

const num = (x: unknown, d = 0) => (typeof x === 'number' && isFinite(x) ? x : d)

export function trend(now: number, before: number): 'up' | 'down' | 'flat' {
  if (before <= 0) return now > 0 ? 'up' : 'flat'
  return now > before * 1.1 ? 'up' : now < before * 0.9 ? 'down' : 'flat'
}

export function summarize(overview: any, alerts: Alert[], at: number): Summary {
  const cur: Window = overview?.current ?? {}
  const prev: Window = overview?.previous ?? {}
  const pace: Pace[] = overview?.limits?.pace ?? []
  const week = pace[0] ?? {}
  const five = pace[1] ?? {}
  const live: Live[] = (overview?.live ?? [])
    .filter((r: any) => num(r.burn30) >= 0.01)
    .sort((a: any, b: any) => num(b.burn30) - num(a.burn30))
    .slice(0, 3)
    .map((r: any) => ({ name: String(r.name ?? r.id ?? '?'), burn30: num(r.burn30), ctxK: Math.round(num(r.context) / 1000) }))
  const active = alerts.filter(a => a.active)
  const cost = num(cur.cost)
  return {
    at,
    range: String(overview?.range ?? '24h'),
    allowance: typeof overview?.limits?.allowance === 'number' && overview.limits.allowance > 0 ? overview.limits.allowance : null,
    weeklyPerHour: typeof week.perHour === 'number' ? week.perHour : null,
    weeklyAtReset: typeof week.atReset === 'number' ? week.atReset : null,
    limitAt: typeof week.limitAt === 'number' ? week.limitAt : null,
    fivePerHour: typeof five.perHour === 'number' ? five.perHour : null,
    costPerHour: num(cur.costPerHour),
    rateTrend: trend(num(cur.costPerHour), num(prev.costPerHour)),
    commits: num(cur.commits),
    costPerCommit: num(cur.commits) > 0 ? num(cur.costPerCommit) : null,
    commitsPerHour: num(cur.commitsPerHour),
    cacheHit: typeof cur.cacheHit === 'number' ? cur.cacheHit : null,
    churn: num(cur.churn),
    big: num(cur.big),
    bigShare: cost > 0 ? num(cur.big) / cost : 0,
    live,
    alerts: active.length,
    alert: active[0] ? String(active[0].title ?? active[0].text ?? '') : null,
    alertKey: active[0]?.key ?? null,
  }
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const pad = (n: number) => String(n).padStart(2, '0')

export function when(ts: number): string {
  const d = new Date(ts * 1000)
  return `${DAYS[d.getDay()]} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export const pct = (f: number, nd = 0) => `${(f * 100).toFixed(nd)}%`
export const usd = (x: number) => `$${x >= 100 ? x.toFixed(0) : x.toFixed(2)}`
/** A cost as a share of the weekly limit ("1.4%") when the allowance is known, else dollars. */
export const cost = (s: Pick<Summary, 'allowance'>, x: number) => {
  if (!s.allowance) return usd(x)
  const v = (100 * x) / s.allowance
  return `${v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : v.toFixed(2)}%`
}
export const arrow = (t: Summary['rateTrend']) => (t === 'up' ? '▲' : t === 'down' ? '▼' : '=')

/** The pace text (weekly burn per hour, projected share at the reset), when the limit is hit (if before the reset),
 * and whether it is over the limit at the reset. */
export function paceText(s: Summary): { text: string; limit: string | null; level: 'ok' | 'warn' | 'over' } {
  if (s.weeklyPerHour === null) return { text: 'no plan reading yet', limit: null, level: 'ok' }
  const at = s.weeklyAtReset ?? 0
  return { text: `${pct(s.weeklyPerHour, 2)}/h → ${pct(at)} at reset`,
    limit: at > 1 && s.limitAt ? `limit hit ${when(s.limitAt)}` : null,
    level: at > 1 ? 'over' : at > 0.85 ? 'warn' : 'ok' }
}
