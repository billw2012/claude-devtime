export type Live = { name: string; burn30: number; ctxK: number }

export type Summary = {
  at: number
  range: string
  allowance: number | null  // API-equivalent dollars per 100 % of the week, once the plan's readings give one
  weeklyPerHour: number | null
  weeklyAtReset: number | null
  limitAt: number | null
  fivePerHour: number | null
  costPerHour: number
  rateTrend: 'up' | 'down' | 'flat'
  commits: number
  costPerCommit: number | null
  commitsPerHour: number
  cacheHit: number | null
  churn: number
  big: number
  bigShare: number
  live: Live[]
  alerts: number
  alert: string | null
  alertKey: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'devtime': { summary: Summary | null; error: string | null; active: boolean }
  }
}
