function pause(ms, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      reject(signal.reason)
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) abort()
  })
}

export async function readWithRetry(load, { signal, intervalMs = 2000, onProgress } = {}) {
  let failures = 0
  for (;;) {
    signal?.throwIfAborted()
    try {
      return await load(signal)
    } catch (error) {
      signal?.throwIfAborted()
      const status = error?.response?.status
      // Only the status GET is retried. Never resubmit the upload or inference.
      if (error?.name === 'AbortError' || (status && status < 500) || ++failures > 3) throw error
      onProgress?.({ status: 'processing', message: 'Reconnecting to generation task...' })
      await pause(intervalMs, signal)
    }
  }
}

export async function waitForTask(load, { signal, intervalMs = 2000, onProgress } = {}) {
  for (;;) {
    const response = await readWithRetry(load, { signal, intervalMs, onProgress })
    signal?.throwIfAborted()
    const task = response?.data
    if (!response?.success || !task) throw new Error(response?.error || 'Task status unavailable')
    if (task.status === 'completed') {
      if (!task.result) throw new Error('Task completed without a result')
      return { success: true, data: task.result }
    }
    if (task.status === 'failed') throw new Error(task.error || 'Ontology generation failed')
    if (!['pending', 'processing'].includes(task.status)) throw new Error('Unknown task status')
    onProgress?.(task)
    await pause(intervalMs, signal)
  }
}

export function startSerialPolling(load, { intervalMs = 2000, onError } = {}) {
  const controller = new AbortController()
  let timer
  const tick = async () => {
    try {
      await load(controller.signal)
    } catch (error) {
      if (!controller.signal.aborted) onError?.(error)
    } finally {
      if (!controller.signal.aborted) timer = setTimeout(tick, intervalMs)
    }
  }
  tick()
  return () => {
    controller.abort()
    clearTimeout(timer)
  }
}
