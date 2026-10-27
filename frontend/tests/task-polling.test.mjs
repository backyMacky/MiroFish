import assert from 'node:assert/strict'
import test from 'node:test'
import { waitForTask, startSerialPolling } from '../src/api/taskPolling.js'

const response = (status, extra = {}) => ({ success: true, data: { status, ...extra } })
test('slow tasks yield progress and resolve only after a confirmed result', async () => {
  const states = [response('pending'), response('processing'), response('completed', { result: { project_id: 'owned-test', ontology: {} } })]
  const progress = []
  const result = await waitForTask(async () => states.shift(), { intervalMs: 0, onProgress: (task) => progress.push(task.status) })
  assert.deepEqual(progress, ['pending', 'processing'])
  assert.equal(result.data.project_id, 'owned-test')
})

test('failure, missing status and empty completion remain failures', async () => {
  await assert.rejects(waitForTask(async () => response('failed', { error: 'Provider unavailable' })), /Provider unavailable/)
  await assert.rejects(waitForTask(async () => ({ success: false, error: 'Task not found' })), /Task not found/)
  await assert.rejects(waitForTask(async () => response('completed')), /without a result/)
  await assert.rejects(waitForTask(async () => response('unexpected')), /Unknown task status/)
})

test('unmount cancellation stops the wait and sends no additional status requests', async () => {
  const controller = new AbortController()
  let calls = 0
  const waiting = waitForTask(async () => { calls++; return response('processing') }, {
    signal: controller.signal, intervalMs: 60_000, onProgress: () => controller.abort(),
  })
  await assert.rejects(waiting, { name: 'AbortError' })
  assert.equal(calls, 1)
})

test('temporary status transport failures recover without submitting work again', async () => {
  let calls = 0
  const result = await waitForTask(async () => {
    calls++
    if (calls === 1) throw Object.assign(new Error('Gateway unavailable'), { response: { status: 502 } })
    return response('completed', { result: { project_id: 'same-project' } })
  }, { intervalMs: 0 })
  assert.equal(calls, 2)
  assert.equal(result.data.project_id, 'same-project')
})

test('authentication failures and repeated outages terminate status polling', async () => {
  let calls = 0
  await assert.rejects(waitForTask(async () => {
    calls++
    throw Object.assign(new Error('Sign in required'), { response: { status: 401 } })
  }, { intervalMs: 0 }), /Sign in/)
  assert.equal(calls, 1)
  calls = 0
  await assert.rejects(waitForTask(async () => { calls++; throw new Error('Connection lost') }, { intervalMs: 0 }), /Connection lost/)
  assert.equal(calls, 4)
})

test('an ignored cancellation cannot publish a late completed result', async () => {
  const controller = new AbortController()
  await assert.rejects(waitForTask(async () => {
    controller.abort()
    return response('completed', { result: { project_id: 'stale-project' } })
  }, { signal: controller.signal }), { name: 'AbortError' })
})

test('serial polling never overlaps slow requests and cancellation prevents rescheduling', async () => {
  let active = 0
  let calls = 0
  let maximum = 0
  let release
  let signal
  const stop = startSerialPolling(async (currentSignal) => {
    signal = currentSignal
    calls++
    active++
    maximum = Math.max(maximum, active)
    await new Promise(resolve => { release = resolve })
    active--
  }, { intervalMs: 0 })
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(calls, 1)
  stop()
  assert.equal(signal.aborted, true)
  release()
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(calls, 1)
  assert.equal(maximum, 1)
})
