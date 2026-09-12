import service from './index'

const DEFAULT_UPLOAD_CHUNK_BYTES = (() => {
  const parsed = Number(import.meta.env.VITE_ONTOLOGY_UPLOAD_CHUNK_BYTES)
  if (Number.isFinite(parsed) && parsed > 0) {
    return parsed
  }
  return 8 * 1024 * 1024 // 8MB default request chunk
})()

const buildFileChunks = (files, maxChunkBytes) => {
  const chunks = []
  let currentChunk = []
  let currentBytes = 0

  files.forEach((file) => {
    const fileSize = Number(file?.size) || 0
    const shouldSplit = currentChunk.length > 0 && currentBytes + fileSize > maxChunkBytes

    if (shouldSplit) {
      chunks.push(currentChunk)
      currentChunk = []
      currentBytes = 0
    }

    currentChunk.push(file)
    currentBytes += fileSize

    // If a single file is larger than the chunk budget, upload it alone.
    if (fileSize > maxChunkBytes) {
      chunks.push(currentChunk)
      currentChunk = []
      currentBytes = 0
    }
  })

  if (currentChunk.length > 0) {
    chunks.push(currentChunk)
  }

  return chunks
}

/**
 * Generate ontology from uploaded documents.
 * @param {FormData} formData
 * @returns {Promise}
 */
export function generateOntology(formData) {
  return service({
    url: '/api/graph/ontology/generate',
    method: 'post',
    data: formData,
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
}

/**
 * Chunked ontology upload that keeps one project_id across multiple requests.
 * @param {Object} options
 * @param {File[]} options.files
 * @param {String} options.simulationRequirement
 * @param {String} [options.projectName]
 * @param {String} [options.additionalContext]
 * @param {Number} [options.maxChunkBytes]
 * @param {Function} [options.onChunkStart]
 * @returns {Promise}
 */
export async function generateOntologyChunked({
  files = [],
  simulationRequirement = '',
  projectName = '',
  additionalContext = '',
  maxChunkBytes = DEFAULT_UPLOAD_CHUNK_BYTES,
  onChunkStart = null
} = {}) {
  if (!Array.isArray(files) || files.length === 0) {
    throw new Error('No files selected for ontology generation')
  }

  const chunkBudget = Number.isFinite(maxChunkBytes) && maxChunkBytes > 0
    ? maxChunkBytes
    : DEFAULT_UPLOAD_CHUNK_BYTES
  const chunkQueue = buildFileChunks(files, chunkBudget).map(chunk => [...chunk])

  let currentProjectId = null
  let finalResponse = null
  let uploadedChunks = 0

  while (chunkQueue.length > 0) {
    const chunkFiles = chunkQueue.shift()
    const deferOntology = chunkQueue.length > 0
    const chunkBytes = chunkFiles.reduce((sum, file) => sum + (Number(file?.size) || 0), 0)
    const totalChunks = uploadedChunks + chunkQueue.length + 1

    if (typeof onChunkStart === 'function') {
      onChunkStart({
        chunkIndex: uploadedChunks + 1,
        totalChunks,
        fileCount: chunkFiles.length,
        chunkBytes,
        deferOntology
      })
    }

    try {
      const formData = new FormData()
      chunkFiles.forEach((file) => {
        formData.append('files', file)
      })

      if (simulationRequirement) {
        formData.append('simulation_requirement', simulationRequirement)
      }
      if (projectName) {
        formData.append('project_name', projectName)
      }
      if (additionalContext) {
        formData.append('additional_context', additionalContext)
      }
      if (currentProjectId) {
        formData.append('project_id', currentProjectId)
      }
      if (deferOntology) {
        formData.append('defer_ontology', 'true')
      }

      finalResponse = await generateOntology(formData)
      if (!finalResponse?.success) {
        throw new Error(finalResponse?.error || 'Ontology generation failed')
      }

      currentProjectId = finalResponse?.data?.project_id || currentProjectId
      uploadedChunks += 1
    } catch (error) {
      const status = error?.response?.status
      if (status === 413 && chunkFiles.length > 1) {
        const mid = Math.ceil(chunkFiles.length / 2)
        const left = chunkFiles.slice(0, mid)
        const right = chunkFiles.slice(mid)
        chunkQueue.unshift(right)
        chunkQueue.unshift(left)
        continue
      }

      if (status === 413 && chunkFiles.length === 1) {
        const blockedFile = chunkFiles[0]
        const fileName = blockedFile?.name || 'unknown-file'
        const fileSizeMb = ((Number(blockedFile?.size) || 0) / (1024 * 1024)).toFixed(2)
        throw new Error(
          `Upload rejected with 413 for "${fileName}" (${fileSizeMb} MB). Single file exceeds upstream request limit.`
        )
      }

      throw error
    }
  }

  return finalResponse
}

/**
 * Build graph from project id.
 * @param {Object} data
 * @returns {Promise}
 */
export function buildGraph(data) {
  return service({
    url: '/api/graph/build',
    method: 'post',
    data
  })
}

/**
 * Query task status.
 * @param {String} taskId
 * @returns {Promise}
 */
export function getTaskStatus(taskId) {
  return service({
    url: `/api/graph/task/${taskId}`,
    method: 'get'
  })
}

/**
 * Fetch graph data.
 * @param {String} graphId
 * @returns {Promise}
 */
export function getGraphData(graphId) {
  return service({
    url: `/api/graph/data/${graphId}`,
    method: 'get'
  })
}

/**
 * Fetch project details.
 * @param {String} projectId
 * @returns {Promise}
 */
export function getProject(projectId) {
  return service({
    url: `/api/graph/project/${projectId}`,
    method: 'get'
  })
}
