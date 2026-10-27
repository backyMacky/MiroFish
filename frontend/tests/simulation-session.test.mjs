import assert from 'node:assert/strict'
import test from 'node:test'
import { openSimulationSession } from '../src/api/simulationSession.js'

for (const status of ['running', 'paused', 'stopping', 'completed', 'stopped', 'failed']) {
  test(`navigation reconnects to ${status} without replaying start`, async () => {
    const existing = { success: true, data: { runner_status: status, process_pid: 123 } }
    const result = await openSimulationSession(async () => existing, () => assert.fail('Must preserve existing run'), { simulation_id: 'synthetic' })
    assert.equal(result, existing)
  })
}

test('only idle state starts once, with force disabled', async () => {
  let calls = 0
  await openSimulationSession(async () => ({ success: true, data: { runner_status: 'idle' } }), async parameters => {
    calls++
    assert.equal(parameters.force, false)
    assert.equal(parameters.max_rounds, 2)
  }, { simulation_id: 'synthetic', max_rounds: 2, force: true })
  assert.equal(calls, 1)
})

test('failed status reads and unknown states never trigger a mutation', async () => {
  for (const state of [{ success: false, error: 'Unavailable' }, { success: true, data: { runner_status: 'unknown' } }]) {
    await assert.rejects(openSimulationSession(async () => state, () => assert.fail('Must not start'), { simulation_id: 'synthetic' }))
  }
})
