import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionRateLimit } from 'claude-code'

import { readingOf } from './readings'
import { arrow, cost, paceText, pct, summarize } from './summary'

// devtime's perf numbers (pace against the weekly limit, $/h, $/commit, waste, live burners, alerts), read from the
// local devtime server every minute. No model calls. Only in the devtime session: the one that ran /devtime:start (or
// /devtime-panel); every other session gets nothing drawn, no polling and no toasts. `/devtime-panel off` ends it.

const PANE = 'devtime'
const TITLE = 'devtime'
// What a click opens: the desktop app makes a link only of https: or http://localhost
let DASHBOARD = 'http://localhost:8765/'
let URL_BASE = 'http://127.0.0.1:8765'
const EVERY_MS = 60_000
const COLUMNS = 46

const summary = atom({ plugin: 'devtime', key: 'summary' } as const, null)
const error = atom({ plugin: 'devtime', key: 'error' } as const, null)
const active = atom({ plugin: 'devtime', key: 'active' } as const, false)
const START = /^\/?devtime:start/

let timer: { cancel: () => void } | null = null

let lastAlertKey: string | null | undefined

const DEFAULT_PORT = 8765

/** The dashboard's port: <data dir>/cache/port.txt (data dir = DEVTIME_HOME, else ~/.claude/devtime), else 8765. */
async function dashboardPort($: EngineInterface): Promise<number> {
  try {
    let dir = await $.env.get('DEVTIME_HOME')
    if (!dir) {
      const home = (await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))
      if (!home) return DEFAULT_PORT
      dir = home.replace(/[\/]+$/, '') + '/.claude/devtime'
    }
    const text = await $.fs.read(dir.replace(/[\/]+$/, '') + '/cache/port.txt')
    const n = parseInt(String(text).trim(), 10)
    return n > 0 && n < 65536 ? n : DEFAULT_PORT
  } catch {
    return DEFAULT_PORT
  }
}

let lastKey = ''

/** Posts a reading to the server; never throws. Identical consecutive readings of this session are not sent twice. */
async function postReading($: EngineInterface, windows: SessionRateLimit[]): Promise<void> {
  try {
    const session = await $.session.id()
    const r = readingOf(session, Date.now() / 1000, windows)
    if (!r) return
    const key = JSON.stringify([r.session, r.fiveHour, r.fiveHourReset, r.weekly, r.weeklyReset])
    if (key === lastKey) return
    const port = await dashboardPort($)
    const res = await $.http.fetch(`http://127.0.0.1:${port}/api/reading`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(r) })
    if (res.ok) lastKey = key  // a failed post (server down) is tried again with the next measurement
  } catch {
    // silent by design
  }
}

async function getJson($: EngineInterface, path: string) {
  const port = await dashboardPort($)  // from <data dir>/cache/port.txt
  URL_BASE = `http://127.0.0.1:${port}`
  DASHBOARD = `http://localhost:${port}/`
  const res = await $.http.fetch(URL_BASE + path)
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return JSON.parse(res.text)
}

async function refresh($: EngineInterface) {
  try {
    const [overview, alerts] = await Promise.all([getJson($, '/api/overview'), getJson($, '/api/alerts')])
    const s = summarize(overview, alerts?.alerts ?? [], Date.now() / 1000)
    await update($, summary, () => s)
    await update($, error, () => null)
    if (lastAlertKey !== undefined && s.alertKey && s.alertKey !== lastAlertKey && s.alert) $.ui.toast(`devtime: ${s.alert}`)
    lastAlertKey = s.alertKey
  } catch (e) {
    await update($, error, () => 'devtime server not answering on ' + URL_BASE)
  }
}

/** Makes this the devtime session: polling, the band and toasts start (idempotent). */
async function activate($: EngineInterface) {
  await update($, active, () => true)
  if (!timer) {
    timer = $.clock.every(EVERY_MS, () => void refresh($))
    void refresh($)
  }
}

export const register: Register = on => {
  // Every session (CLI and desktop), silently: report the plan's usage windows to the devtime server. Observe only.
  on('session.measure', async ($, e, next) => {
    if (e.rateLimits.length > 0 && e.changed.includes('rateLimits')) await postReading($, e.rateLimits)
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'devtime-panel', description: 'devtime perf view in this session (off: stop)' })
    $.ui.status(undefined)  // no status entry anywhere (clears one an earlier version left)
    if (await read($, active)) await activate($)  // a reload, or a resumed devtime session
    return next(e)
  })

  // /devtime:start (and its loop's ticks) mark the devtime session.
  on('prompt.submit', async ($, e, next) => {
    if (START.test(e.text.trim())) await activate($)
    return next(e)
  }).catch(($, e, next) => next(e))
  on('skill.prompt', async ($, e, next) => {
    if (START.test(e.skill)) await activate($)
    return next(e)
  })

  on('command.run', { command: 'devtime-panel' }, async ($, e) => {
    if ((e.args ?? '').trim() === 'off') {
      timer?.cancel()
      timer = null
      await update($, active, () => false)
      return { text: 'devtime view off in this session.' }
    }
    await activate($)
    await refresh($)
    const opened = await $.ui.open({ id: PANE, title: TITLE, columns: COLUMNS })
    return { text: opened.isPlaced ? 'devtime pane opened.' : `devtime pane waits: ${opened.reason}` }
  })

  // One line above the prompt; the pane (its "details" button, or /devtime-panel) has the rest.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, summary)
    if (e.props.hasSurvey || !s || !(await read($, active))) return next(e)
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const pace = paceText(s)
    const paceColor = pace.level === 'over' ? 'error' : pace.level === 'warn' ? 'warning' : undefined
    return (
      <Box flexDirection="row" gap={1}>
        <Box flexShrink={1}>
          <Text wrap="truncate-end">
            <Text dimColor>devtime </Text>
            <Text color={paceColor}>{pace.text}{pace.limit ? ` (${pace.limit})` : ''}</Text>
            <Text> · {cost(s, s.costPerHour)}/h {arrow(s.rateTrend)} · {s.costPerCommit === null ? '-' : cost(s, s.costPerCommit)}/commit · cache {s.cacheHit === null ? '-' : pct(s.cacheHit)}</Text>
            {s.alerts ? <Text color="warning"> · {s.alerts} alert{s.alerts > 1 ? 's' : ''}</Text> : null}
          </Text>
        </Box>
        <Button key="details" plain onPress={() => void $.ui.open({ id: PANE, title: TITLE, columns: COLUMNS })}>details</Button>
        <Link href={DASHBOARD} label="dashboard" />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const s = await read($, summary)
    const err = await read($, error)
    // Narrow by design (COLUMNS wide): a dim label column, then short lines that wrap.
    const row = (label: string, ...lines: any[]) => (
      <Box flexDirection="row">
        <Box width={6} flexShrink={0}><Text dimColor>{label}</Text></Box>
        <Box flexDirection="column" flexShrink={1}>{lines}</Box>
      </Box>
    )
    const header = (
      <Box flexDirection="row" gap={1} flexWrap="wrap">
        <Text bold>{s ? s.range : 'devtime'}</Text>
        <Link href={DASHBOARD} label="dashboard" />
        <Button key="refresh" plain onPress={() => void refresh($)}>↻</Button>
      </Box>
    )
    if (!s) {
      return (
        <Box flexDirection="column">
          {header}
          <Text dimColor wrap="wrap">{err ?? 'reading the devtime server...'}</Text>
        </Box>
      )
    }
    const pace = paceText(s)
    const paceColor = pace.level === 'over' ? 'error' : pace.level === 'warn' ? 'warning' : undefined
    return (
      <Box flexDirection="column">
        {header}
        {err && <Text color="warning" wrap="wrap">server not answering; last reading</Text>}
        {row('Pace', <Text color={paceColor} wrap="wrap">{pace.text}</Text>,
          pace.limit && <Text color={paceColor}>{pace.limit}</Text>)}
        {row('Rate', <Text wrap="wrap">{cost(s, s.costPerHour)}/h {arrow(s.rateTrend)}{s.fivePerHour === null ? '' : `  5h ${pct(s.fivePerHour, 2)}/h`}</Text>)}
        {row('Out', <Text wrap="wrap">{s.commits} commits · {s.costPerCommit === null ? '-' : cost(s, s.costPerCommit)} each</Text>)}
        {row('Waste', <Text wrap="wrap">cache {s.cacheHit === null ? '-' : pct(s.cacheHit)} · rewrites {cost(s, s.churn)}</Text>,
          <Text wrap="wrap">big ctx {cost(s, s.big)} ({pct(s.bigShare)})</Text>)}
        {row('Live', ...(s.live.length === 0
          ? [<Text dimColor>nothing burning</Text>]
          : s.live.map(l => <Text wrap="truncate-end">{cost(s, l.burn30)}/30m {l.ctxK}k {l.name}</Text>)))}
        {row('Alert', s.alerts
          ? <Text color="warning" wrap="wrap">{s.alerts} · {(s.alert ?? '').slice(0, 80)}</Text>
          : <Text dimColor>none</Text>)}
      </Box>
    )
  })
}
