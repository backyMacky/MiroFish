import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { setImmediate } from 'node:timers/promises'
import { createServer } from 'vite'
import { createRenderer, h, nextTick, reactive, ssrContextKey } from 'vue'
import { AxiosError } from 'axios'

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

// Compile the real SFC and exercise reactive state/lifecycle without a DOM.
// Only HTTP and the rendering host are fixtures; production event handlers run.
async function mountInteraction(handle) {
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
    const component = (await server.ssrLoadModule('/src/components/Step5Interaction.vue')).default
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
    app.provide(ssrContextKey, { modules: new Set() })
    app.mixin({ created() { if (this.$options.__name === 'Step5Interaction') instance = this.$ } })
    app.mount({})
    await setImmediate()
    return { state: instance.setupState, props, requests, service, close }
  } catch (error) {
    await close()
    throw error
  }
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
