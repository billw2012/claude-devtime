// Plan-usage readings from every session. The engine hands each session its rate-limit windows (five_hour, seven_day:
// percent used and reset time); this module posts them to the local devtime server (POST /api/reading), which keeps
// them as plan samples. Numbers and the session id only; nothing is drawn and every failure is ignored.

export type Window = { kind: string; percentUsed: number; resetsAt?: string }
export type Reading = { session: string; at: number; fiveHour?: number; fiveHourReset?: string; weekly?: number; weeklyReset?: string }

/** The reading to post for these windows, or null when neither the 5-hour nor the weekly window is there. */
export function readingOf(session: string, at: number, windows: Window[]): Reading | null {
  const five = windows.find(w => w.kind === 'five_hour' && typeof w.percentUsed === 'number')
  const week = windows.find(w => w.kind === 'seven_day' && typeof w.percentUsed === 'number')
  if (!five && !week) return null
  const r: Reading = { session, at }
  if (five) { r.fiveHour = five.percentUsed; if (five.resetsAt) r.fiveHourReset = five.resetsAt }
  if (week) { r.weekly = week.percentUsed; if (week.resetsAt) r.weeklyReset = week.resetsAt }
  return r
}
