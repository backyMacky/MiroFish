import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { setImmediate } from 'node:timers/promises'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { AxiosError } from 'axios'
import { routerKey, routeLocationKey } from 'vue-router'

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

// Compile the real SFC and exercise reactive state/lifecycle without a DOM.
// Only HTTP and the rendering host are fixtures; production event handlers run.
async function mountInteraction(handle, componentName = 'Step5Interaction') {
  globalThis.localStorage = { getItem: () => 'en' }
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector: () => null }
  const root = fileURLToPath(new URL('../', import.meta.url))
  const server = await createServer({
    root, logLevel: 'error', optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, watch: null },
  })
  const requests = []
  let instance, app
  const close = async () => {
    app?.unmount()
    await server.close()
    delete globalThis.localStorage
    delete globalThis.document
  }
  try {
    const service = (await server.ssrLoadModule('/src/api/index.js')).default
    service.defaults.adapter = async config => {
      requests.push(config)
      let data = await handle(config)
      if (data === undefined) {
        if (config.url.includes('/profiles/realtime')) data = { profiles: [{ username: 'Avery' }, { username: 'Morgan' }] }
        else if (config.method === 'get' && config.url.startsWith('/api/report/')) data = { logs: [] }
        else throw new Error(`Unexpected fixture route: ${config.method} ${config.url}`)
      }
      return { status: 200, statusText: 'OK', config, headers: {}, data: { success: true, data } }
    }
    const directory = componentName.endsWith('View') ? 'views' : 'components'
    const component = (await server.ssrLoadModule(`/src/${directory}/${componentName}.vue`)).default
    const i18n = (await server.ssrLoadModule('/src/i18n/index.js')).default
    const renderer = createRenderer({
      createElement: () => ({}), createComment: () => ({}), createText: () => ({}),
      insert() {}, remove() {}, patchProp() {}, setElementText() {}, setText() {},
      parentNode: () => null, nextSibling: () => null,
    })
    const props = reactive({ simulationId: 'sim_owned', reportId: 'report_owned' })
    const stateComponent = { ...component, render: () => null }
    app = renderer.createApp({ setup: () => () => h(stateComponent, { simulationId: props.simulationId, reportId: props.reportId }) })
    app.use(i18n)
    const navigations = []
    const route = reactive({ params: { reportId: 'report_owned' } })
    app.provide(routerKey, { push(location) { navigations.push(location) } })
    app.provide(routeLocationKey, route)
    app.provide(ssrContextKey, { modules: new Set() })
    app.mixin({ created() { if (this.$options.__name === componentName) instance = this.$ } })
    app.mount({})
    await setImmediate()
    return { state: instance.setupState, props, requests, service, navigations, route, close }
  } catch (error) {
    await close()
    throw error
  }
}

for (const terminal of ['completed', 'stopped', 'failed']) {
  test(`terminal ${terminal} status refreshes final actions before canceling detail polling`, async () => {
    const initialDetail = deferred(), detailStarted = deferred()
    let terminalVisible = false, detailCount = 0
    const fixture = await mountInteraction(async config => {
      if (config.url.endsWith('/run-status')) return { runner_status: terminalVisible ? terminal : 'running' }
      if (config.url.endsWith('/run-status/detail')) {
        if (++detailCount === 1) {
          detailStarted.resolve()
          await initialDetail.promise
          return { all_actions: [] }
        }
        return { all_actions: [
          { id: 'twitter-tail', agent_id: 0, platform: 'twitter', action_type: 'CREATE_POST' },
          { id: 'reddit-tail', agent_id: 1, platform: 'reddit', action_type: 'CREATE_POST' },
        ] }
      }
    }, 'Step3Simulation')
    try {
      await detailStarted.promise
      terminalVisible = true
      await fixture.state.fetchRunStatus()
      assert.deepEqual(fixture.state.allActions.map(action => action.id), ['twitter-tail', 'reddit-tail'])
      assert.equal(fixture.state.phase, terminal === 'failed' ? 0 : 2)
      assert.equal(detailCount, 2, 'Final action logs must be read even if the earlier detail poll is incomplete')
    } finally { initialDetail.resolve(); await fixture.close() }
  })
}

test('a delayed report reply stays with its original conversation after selecting an agent', async () => {
  const started = deferred(), reply = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.method === 'post' && config.url === '/api/report/chat') {
      started.resolve()
      await reply.promise
      return { response: 'Synthetic report reply' }
    }
  })
  try {
    const { state } = fixture
    state.chatInput = 'Synthetic report question'
    const pending = state.sendMessage()
    await started.promise
    state.toggleAgentDropdown()
    state.selectAgent({ username: 'Morgan' }, 1)
    reply.resolve()
    await pending
    assert.deepEqual(state.chatHistory, [], 'The agent chat must not receive the report reply')
    state.selectReportAgentChat()
    assert.deepEqual(state.chatHistory.map(item => item.content), ['Synthetic report question', 'Synthetic report reply'])
  } finally { reply.resolve(); await fixture.close() }
})

test('a delayed agent reply returns to that agent after switching to report chat', async () => {
  const started = deferred(), reply = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.method === 'post' && config.url === '/api/simulation/interview/batch') return { command_id: 'owned_command' }
    if (config.url.includes('/interview/status/')) {
      started.resolve()
      await reply.promise
      return { status: 'completed', result: { result: { results: { reddit_0: { agent_id: 0, response: 'Synthetic Avery reply' } } } } }
    }
  })
  try {
    const { state } = fixture
    state.selectAgent({ username: 'Avery' }, 0)
    state.chatInput = 'Synthetic Avery question'
    const pending = state.sendMessage()
    await started.promise
    state.selectReportAgentChat()
    reply.resolve()
    await pending
    assert.deepEqual(state.chatHistory, [])
    state.selectAgent({ username: 'Avery' }, 0)
    assert.deepEqual(state.chatHistory.map(item => item.content), ['Synthetic Avery question', 'Synthetic Avery reply'])
    assert.equal(fixture.requests.filter(item => item.method === 'post').length, 1)
  } finally { reply.resolve(); await fixture.close() }
})

test('changing simulation and report discards late replies and old conversation caches', async () => {
  const started = deferred(), reply = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.method === 'post' && config.url === '/api/report/chat') {
      started.resolve()
      await reply.promise
      return { response: 'Old simulation reply' }
    }
  })
  try {
    const { state, props } = fixture
    state.chatInput = 'Old simulation question'
    const pending = state.sendMessage()
    await started.promise
    props.simulationId = 'sim_other'
    props.reportId = 'report_other'
    await nextTick()
    reply.resolve()
    await pending
    assert.deepEqual(state.chatHistory, [])
    assert.deepEqual(state.chatHistoryCache, {})
    assert.equal(state.isSending, false)
  } finally { reply.resolve(); await fixture.close() }
})

test('a survey retains the submitted question and agents while its result is pending', async () => {
  const started = deferred(), reply = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.method === 'post' && config.url === '/api/simulation/interview/batch') return { command_id: 'survey_command' }
    if (config.url.includes('/interview/status/')) {
      started.resolve()
      await reply.promise
      return { status: 'completed', result: { result: { results: { reddit_0: { agent_id: 0, response: 'Original survey answer' } } } } }
    }
  })
  try {
    const { state } = fixture
    state.selectSurveyTab()
    state.selectedAgents = new Set([0])
    state.surveyQuestion = 'Original survey question'
    const pending = state.submitSurvey()
    await started.promise
    state.surveyQuestion = 'Later input'
    state.selectedAgents = new Set([1])
    reply.resolve()
    await pending
    assert.equal(state.surveyResults[0].question, 'Original survey question')
    assert.equal(state.surveyResults[0].agent_name, 'Avery')
    assert.equal(state.surveyResults[0].answer, 'Original survey answer')
  } finally { reply.resolve(); await fixture.close() }
})

test('changing context aborts interview polling without resubmitting its command', async t => {
  const started = deferred(), reply = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.method === 'post' && config.url === '/api/simulation/interview/batch') return { command_id: 'retained_command' }
    if (config.url.includes('/interview/status/')) {
      started.resolve()
      await reply.promise
      return { status: 'completed', result: { result: { results: { reddit_0: { agent_id: 0, response: 'Old interview answer' } } } } }
    }
  })
  try {
    const errors = []
    t.mock.method(console, 'error', (...args) => errors.push(args))
    const { state, props } = fixture
    state.selectAgent({ username: 'Avery' }, 0)
    state.chatInput = 'Old interview question'
    const pending = state.sendMessage()
    await started.promise
    props.simulationId = 'sim_new'
    props.reportId = 'report_new'
    await nextTick()
    reply.resolve()
    await pending
    assert.deepEqual(state.chatHistory, [])
    assert.deepEqual(state.chatHistoryCache, {})
    assert.equal(fixture.requests.find(item => item.url.includes('/interview/status/')).signal.aborted, true)
    assert.equal(fixture.requests.filter(item => item.method === 'post').length, 1)
    assert.deepEqual(errors, [])
  } finally { reply.resolve(); await fixture.close() }
})

test('failed reports stop generation indicators and retain logs for explicit retry', async () => {
  const fixture = await mountInteraction(async config => {
    if (config.url.endsWith('/agent-log')) return { status: 'failed', elapsed_seconds: 901.4, total_lines: 3, logs: [
      { action: 'report_start' },
      { action: 'section_start', section_index: 1 },
      { action: 'error', stage: 'failed', details: { error: 'Provider timed out' } },
    ] }
  }, 'Step4Report')
  try {
    const { state } = fixture
    assert.equal(state.isFailed, true)
    assert.equal(state.isComplete, false)
    assert.equal(state.failureMessage, 'Provider timed out')
    assert.equal(state.currentSectionIndex, null)
    assert.equal(state.activeSectionIndex, null)
    assert.equal(state.activeStep.status, 'failed')
    assert.equal(state.statusClass, 'failed')
    assert.equal(state.formatElapsedTime, '15m 1s')
    assert.equal(state.agentLogs.length, 3)
    assert.equal(fixture.requests.filter(r => r.method === 'post').length, 0)
    assert.ok(fixture.requests.filter(r => r.url.includes('-log')).every(r => r.signal.aborted))
  } finally { await fixture.close() }
})

test('report elapsed time advances on empty log polls and rounds minute boundaries', async () => {
  let elapsed = 59.6
  const fixture = await mountInteraction(async config => {
    if (config.url.endsWith('/agent-log')) return { logs: [], total_lines: 7, elapsed_seconds: elapsed }
  }, 'Step4Report')
  try {
    assert.equal(fixture.state.formatElapsedTime, '1m 0s')
    assert.equal(fixture.state.agentLogLine, 7)
    elapsed = 124
    await fixture.state.fetchAgentLog()
    assert.equal(fixture.state.formatElapsedTime, '2m 4s')
    assert.equal(fixture.state.agentLogs.length, 0)
  } finally { await fixture.close() }
})

test('report navigation aborts old polls and ignores their late logs', async () => {
  const reply = deferred(), started = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.url.includes('report_owned/agent-log')) {
      started.resolve()
      await reply.promise
      return { logs: [{ action: 'report_complete' }], status: 'completed' }
    }
    if (config.url.includes('report_new/agent-log')) return { logs: [], elapsed_seconds: 3 }
  }, 'Step4Report')
  try {
    await started.promise
    fixture.props.reportId = 'report_new'
    await nextTick()
    reply.resolve()
    await setImmediate()
    assert.equal(fixture.state.isComplete, false)
    assert.equal(fixture.state.elapsedSeconds, 3)
    assert.deepEqual(fixture.state.agentLogs, [])
    assert.ok(fixture.requests.find(r => r.url.includes('report_owned/agent-log')).signal.aborted)
  } finally { reply.resolve(); await fixture.close() }
})

test('report retry submits once and navigates to the newly accepted report', async () => {
  const reply = deferred(), started = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.url.endsWith('/agent-log')) return { status: 'failed', logs: [] }
    if (config.method === 'post' && config.url === '/api/report/generate') {
      started.resolve()
      await reply.promise
      return { report_id: 'report_retry' }
    }
  }, 'Step4Report')
  try {
    const pending = fixture.state.retryReport()
    await started.promise
    await fixture.state.retryReport()
    assert.equal(fixture.state.isRetrying, true)
    reply.resolve()
    await pending
    const mutations = fixture.requests.filter(r => r.method === 'post')
    assert.equal(mutations.length, 1)
    assert.deepEqual(JSON.parse(mutations[0].data), { simulation_id: 'sim_owned', force_regenerate: true })
    assert.deepEqual(fixture.navigations, [{ name: 'Report', params: { reportId: 'report_retry' } }])
    assert.equal(fixture.state.isRetrying, false)
  } finally { reply.resolve(); await fixture.close() }
})

test('leaving a failed report while retry is accepted does not navigate back', async () => {
  const reply = deferred(), started = deferred()
  const fixture = await mountInteraction(async config => {
    if (config.url.endsWith('/agent-log')) return { status: 'failed', logs: [] }
    if (config.method === 'post') { started.resolve(); await reply.promise; return { report_id: 'report_retry' } }
  }, 'Step4Report')
  try {
    const pending = fixture.state.retryReport()
    await started.promise
    fixture.props.reportId = 'report_other'
    await nextTick()
    reply.resolve()
    await pending
    assert.deepEqual(fixture.navigations, [])
    assert.equal(fixture.requests.filter(r => r.method === 'post').length, 1)
  } finally { reply.resolve(); await fixture.close() }
})

test('report header resets its failure when retry navigates to a new report', async () => {
  const fixture = await mountInteraction(async () => ({}), 'ReportView')
  try {
    fixture.state.currentStatus = 'error'
    fixture.route.params.reportId = 'report_retry'
    await nextTick()
    assert.equal(fixture.state.currentReportId, 'report_retry')
    assert.equal(fixture.state.statusClass, 'processing')
    assert.equal(fixture.state.statusText, 'Generating')
  } finally { await fixture.close() }
})

test('API error logs omit request payloads, credentials and provider response bodies', async t => {
  const fixture = await mountInteraction(async () => undefined)
  try {
    const errors = []
    t.mock.method(console, 'error', (...args) => errors.push(args))
    fixture.service.defaults.adapter = async config => {
      throw new AxiosError('PRIVATE_PROVIDER_BODY', 'ERR_BAD_RESPONSE', config, {}, {
        status: 503, data: { error: 'Synthetic actionable failure' }, config, headers: {},
      })
    }
    await assert.rejects(fixture.service.post('/api/owned-fixture', { prompt: 'PRIVATE_PROMPT' }, {
      headers: { Authorization: 'Bearer SYNTHETIC_CREDENTIAL_DO_NOT_LOG' },
    }), /Synthetic actionable failure/)
    const logged = JSON.stringify(errors)
    for (const privateText of ['PRIVATE_PROVIDER_BODY', 'PRIVATE_PROMPT', 'SYNTHETIC_CREDENTIAL_DO_NOT_LOG']) {
      assert.ok(!logged.includes(privateText))
    }
    assert.ok(logged.includes('503') && logged.includes('ERR_BAD_RESPONSE'))
  } finally { await fixture.close() }
})
