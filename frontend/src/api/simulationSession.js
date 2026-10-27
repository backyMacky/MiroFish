/** Reconnect after navigation; only a genuinely idle simulation may auto-start. */
export async function openSimulationSession(readStatus, start, parameters) {
  const existing = await readStatus(parameters.simulation_id)
  const state = existing?.data
  if (!existing?.success || !state) throw new Error(existing?.error || 'Simulation status unavailable')
  if (state.runner_status === 'idle') return start({ ...parameters, force: false })
  if (!['running', 'paused', 'stopping', 'completed', 'stopped', 'failed'].includes(state.runner_status)) {
    throw new Error('Unknown simulation status; the existing run was preserved')
  }
  return existing
}
