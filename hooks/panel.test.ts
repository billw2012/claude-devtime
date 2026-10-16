import { expect, test } from 'claude-code/testing'

import { cost, paceText, summarize } from './summary'

const OVERVIEW = {
  range: '24h',
  current: { cost: 36, costPerHour: 1.5, commits: 4, costPerCommit: 9, commitsPerHour: 0.17, cacheHit: 0.957, churn: 0, big: 8 },
  previous: { costPerHour: 2.5 },
  limits: { pace: [{ perHour: 0.0084, atReset: 1.2, limitAt: 1792022291 }, { perHour: 0.0012, atReset: 1.0 }] },
  live: [{ name: 'quiet', burn30: 0 }, { name: 'Perf dashboard', burn30: 2.29, context: 114544 }],
}
const ALERTS = { alerts: [{ key: 'a1', active: true, title: 'session X: big context' }, { key: 'a0', active: false }] }

const PANE = {
  component: 'Pane' as const,
  requestId: 'devtime',
  props: { title: 'devtime', isFocused: false, bodyColumns: 90, placement: 'dock' as const,
    scroll: { offset: 0, bodyRows: 12 }, view: {} },
}

const ORG = 'aaaa1111-0000-4000-8000-000000000001'
const OTHER = 'bbbb2222-0000-4000-8000-000000000002'
const seen: string[] = []

/** A session of org ORG whose org's server (port 8123 in its port.txt) answers /api/version as `serverOrg`. */
function host(on: any, fetch: (url: string) => { status: number; ok: boolean; headers: {}; text: string },
  serverOrg = ORG) {
  seen.length = 0
  on('session.id', async () => ({ value: 'sess-1' }))
  on('env.get', async (_$: unknown, e: { name: string }) => ({
    value: e.name === 'DEVTIME_HOME' ? 'C:/data' : e.name === 'CLAUDE_CODE_ORGANIZATION_UUID' ? ORG : undefined }))
  on('fs.read', async (_$: unknown, e: { path: string }) => {
    if (e.path.replace(/\\/g, '/') !== `C:/data/orgs/${ORG}/cache/port.txt`) throw new Error('missing ' + e.path)
    return { value: '8123' }
  })
  on('http.fetch', async (_$: unknown, e: { url: string }) => {
    seen.push(e.url)
    if (e.url.endsWith('/api/version')) return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ org: serverOrg }) } }
    return { value: fetch(e.url) }
  })
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('ui.status', async () => ({ value: undefined }))
  on('ui.toast', async () => ({ value: undefined }))
  on('clock.every', async () => ({ value: undefined }))
}

test('summary reduces the overview', async () => {
  const s = summarize(OVERVIEW, ALERTS.alerts, 0)
  expect(s.rateTrend).toBe('down')
  expect(s.live.length).toBe(1)
  expect(s.live[0]?.ctxK).toBe(115)
  expect(s.alerts).toBe(1)
  expect(paceText(s).level).toBe('over')
  expect(paceText(s).text).toMatch(/120% at reset/)
})

test('pane shows the perf lines on terminal and desktop', async ($, on) => {
  host(on, url => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(url.includes('/api/alerts') ? ALERTS : OVERVIEW) }))
  await $.command.run({ command: 'devtime-panel' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'devtime', surface, ...PANE })
    expect(await ui.find({ type: 'Text', text: /120% at reset/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /\$9\.00 each/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Perf dashboard/ })).toBeDefined()
    expect(await ui.find({ type: 'Link' })).toBeDefined()
    await ui.unmount()
  }
  const data = seen.filter(u => !u.endsWith('/api/version'))
  expect(data.length > 0).toBe(true)
  for (const u of data) {
    expect(u.startsWith('http://127.0.0.1:8123/api/')).toBe(true)
    expect(u.includes(`session=sess-1&org=${ORG}`)).toBe(true)
  }
})

test('pane shows nothing of another org\'s server', async ($, on) => {
  host(on, url => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(url.includes('/api/alerts') ? ALERTS : OVERVIEW) }), OTHER)
  await $.command.run({ command: 'devtime-panel' })
  const ui = await $.ui.mount({ plugin: 'devtime', surface: 'desktop', ...PANE })
  expect(await ui.find({ type: 'Text', text: /Perf dashboard/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /no devtime server of this organisation/ })).toBeDefined()
  expect(seen.filter(u => !u.endsWith('/api/version')).length).toBe(0)
  await ui.unmount()
})

const WITH_ALLOWANCE = { ...OVERVIEW, limits: { ...OVERVIEW.limits, allowance: 3000 } }

test('costs are shares of the week once there is an allowance', async ($, on) => {
  expect(summarize(OVERVIEW, [], 0).allowance).toBe(null)
  const s = summarize(WITH_ALLOWANCE, [], 0)
  expect(s.allowance).toBe(3000)
  expect(cost(s, 9)).toBe('0.30%')
  expect(cost(s, 90)).toBe('3.0%')
  expect(cost(summarize(OVERVIEW, [], 0), 9)).toBe('$9.00')
  host(on, url => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(url.includes('/api/alerts') ? ALERTS : WITH_ALLOWANCE) }))
  await $.command.run({ command: 'devtime-panel' })
  const ui = await $.ui.mount({ plugin: 'devtime', surface: 'desktop', ...PANE })
  expect(await ui.find({ type: 'Text', text: /0\.30% each/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /0\.08%\/30m/ })).toBeDefined()
  await ui.unmount()
})

test('pane says when the server is down', async ($, on) => {
  host(on, () => ({ status: 503, ok: false, headers: {}, text: '' }))
  await $.command.run({ command: 'devtime-panel' })
  const ui = await $.ui.mount({ plugin: 'devtime', surface: 'desktop', ...PANE })
  expect(await ui.find({ type: 'Text', text: /not answering/ })).toBeDefined()
  await ui.unmount()
})

test('band shows one line of pace and rate', async ($, on) => {
  host(on, url => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(url.includes('/api/alerts') ? ALERTS : OVERVIEW) }))
  await $.command.run({ command: 'devtime-panel' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'devtime', surface, component: 'AbovePrompt',
      props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 120, scroll: { offset: 0, bodyRows: 4 } } as any })
    expect(await ui.find({ type: 'Text', text: /120% at reset/ })).toBeDefined()
    expect(await ui.find({ key: 'details' })).toBeDefined()
    await ui.unmount()
  }
})

test('other sessions get no band', async ($, on) => {
  on('ui.render', { component: 'AbovePrompt' }, async ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return h(Text, null, 'engine band')
  })
  host(on, url => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(url.includes('/api/alerts') ? ALERTS : OVERVIEW) }))
  await $.command.run({ command: 'devtime-panel' })
  await $.command.run({ command: 'devtime-panel', args: 'off' })
  const ui = await $.ui.mount({ plugin: 'devtime', surface: 'desktop', component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, maxRows: 4, bodyColumns: 120, scroll: { offset: 0, bodyRows: 4 } } as any })
  expect(await ui.find({ key: 'details' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /engine band/ })).toBeDefined()
  await ui.unmount()
})

const WINDOWS = [{ kind: 'five_hour', percentUsed: 14.5, resetsAt: '2026-10-16T16:10:00Z' },
  { kind: 'seven_day', percentUsed: 74, resetsAt: '2026-10-20T00:00:00Z' }]

function measure(windows: typeof WINDOWS, changed: string[] = ['rateLimits']) {
  return { context: { window: 200000 }, rateLimits: windows, changed } as any
}

const posts: { url: string; body: any }[] = []
const serverState = { down: false, port: '8123', org: ORG, env: ORG as string | undefined, claudeJson: '' }

function readingHost(on: any) {
  posts.length = 0
  on('session.id', async () => ({ value: 'sess-1' }))
  on('session.measure', async () => ({ changed: ['rateLimits'] }))
  on('env.get', async (_$: unknown, e: { name: string }) => ({
    value: e.name === 'DEVTIME_HOME' ? 'C:/data' : e.name === 'USERPROFILE' ? 'C:/Users/u'
      : e.name === 'CLAUDE_CODE_ORGANIZATION_UUID' ? serverState.env : undefined }))
  on('fs.read', async (_$: unknown, e: { path: string }) => {
    const path = e.path.replace(/\\/g, '/')
    if (path === 'C:/Users/u/.claude.json' && serverState.claudeJson) return { value: serverState.claudeJson }
    if (!/^C:\/data\/orgs\/[^/]+\/cache\/port[.]txt$/.test(path)) throw new Error('missing ' + e.path)
    return { value: serverState.port }
  })
  on('http.fetch', async (_$: unknown, e: { url: string; init?: { body?: string } }) => {
    if (serverState.down) throw new Error('connection refused')
    if (e.url.endsWith('/api/version')) return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ org: serverState.org }) } }
    posts.push({ url: e.url, body: e.init?.body ? JSON.parse(e.init.body) : null })
    return { value: { status: 200, ok: true, headers: {}, text: '{}' } }
  })
}

function reset(state: Partial<typeof serverState> = {}) {
  Object.assign(serverState, { down: false, port: '8123', org: ORG, env: ORG, claudeJson: '' }, state)
}

test('measure posts the plan readings to its own org\'s server, in any session', async ($, on) => {
  reset()
  readingHost(on)
  const result = await $.session.measure(measure(WINDOWS))
  expect(result.changed).toEqual(['rateLimits'])
  expect(posts.length).toBe(1)
  expect(posts[0]?.url).toBe('http://127.0.0.1:8123/api/reading')
  expect(posts[0]?.body.session).toBe('sess-1')
  expect(posts[0]?.body.org).toBe(ORG)
  expect(posts[0]?.body.fiveHour).toBe(14.5)
  expect(posts[0]?.body.weekly).toBe(74)
  expect(posts[0]?.body.weeklyReset).toBe('2026-10-20T00:00:00Z')
})

test('measure without plan windows posts nothing', async ($, on) => {
  reset()
  readingHost(on)
  await $.session.measure(measure([]))
  await $.session.measure(measure([{ kind: 'spend_limit', percentUsed: 5 }] as any))
  expect(posts.length).toBe(0)
})

test('measure survives a missing port file and a dead server, and posts nowhere else', async ($, on) => {
  reset({ port: 'junk' })
  readingHost(on)
  const result = await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 51 }] as any))
  expect(result.changed).toEqual(['rateLimits'])
  reset({ down: true })
  await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 52 }] as any))
  expect(posts.length).toBe(0)
})

test('measure posts nothing to another org\'s server or without an org', async ($, on) => {
  reset({ org: OTHER })
  readingHost(on)
  await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 53 }] as any))
  reset({ env: undefined })
  await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 54 }] as any))
  expect(posts.length).toBe(0)
})

test('measure takes the org of the .claude.json login when the env has none', async ($, on) => {
  reset({ env: undefined, claudeJson: JSON.stringify({ oauthAccount: { organizationUuid: ORG } }) })
  readingHost(on)
  await $.session.measure(measure([{ kind: 'seven_day', percentUsed: 55 }] as any))
  expect(posts.length).toBe(1)
  expect(posts[0]?.body.org).toBe(ORG)
})
