<template>
  <div class="main-view">
    <!-- Header -->
    <header class="app-header">
      <div class="header-left">
        <div class="brand" @click="router.push('/')">MIROFISH</div>
      </div>
      
      <div class="header-center">
        <div class="view-switcher">
          <button 
            v-for="mode in ['graph', 'split', 'workbench']" 
            :key="mode"
            class="switch-btn"
            :class="{ active: viewMode === mode }"
            @click="viewMode = mode"
          >
            {{ { graph: $t('main.layoutGraph'), split: $t('main.layoutSplit'), workbench: $t('main.layoutWorkbench') }[mode] }}
          </button>
        </div>
      </div>

      <div class="header-right">
        <LanguageSwitcher />
        <div class="step-divider"></div>
        <div class="workflow-step">
          <span class="step-num">Step {{ currentStep }}/5</span>
          <span class="step-name">{{ $tm('main.stepNames')[currentStep - 1] }}</span>
        </div>
        <div class="step-divider"></div>
        <span class="status-indicator" :class="statusClass">
          <span class="dot"></span>
          {{ statusText }}
        </span>
      </div>
    </header>

    <div v-if="error" class="generation-error" role="alert">
      <span>{{ error }}</span>
      <button v-if="currentProjectId && currentProjectId !== 'new'" :disabled="loading" @click="retryProject">
        {{ $t('common.retry') }}
      </button>
    </div>

    <!-- Main Content Area -->
    <main class="content-area">
      <!-- Left Panel: Graph -->
      <div class="panel-wrapper left" :style="leftPanelStyle" :inert="viewMode === 'workbench'" :aria-hidden="viewMode === 'workbench'">
        <GraphPanel 
          :graphData="graphData"
          :loading="graphLoading"
          :canRefresh="Boolean(projectData?.graph_id)"
          :currentPhase="error ? -1 : currentPhase"
          @refresh="refreshGraph"
          @toggle-maximize="toggleMaximize('graph')"
        />
      </div>

      <!-- Right Panel: Step Components -->
      <div class="panel-wrapper right" :style="rightPanelStyle" :inert="viewMode === 'graph'" :aria-hidden="viewMode === 'graph'">
        <!-- Step 1: 图谱构建 -->
        <Step1GraphBuild 
          v-if="currentStep === 1"
          :currentPhase="currentPhase"
          :projectData="projectData"
          :ontologyProgress="ontologyProgress"
          :buildProgress="buildProgress"
          :graphData="graphData"
          :systemLogs="systemLogs"
          @next-step="handleNextStep"
        />
        <!-- Step 2: 环境搭建 -->
        <Step2EnvSetup
          v-else-if="currentStep === 2"
          :projectData="projectData"
          :graphData="graphData"
          :systemLogs="systemLogs"
          @go-back="handleGoBack"
          @next-step="handleNextStep"
          @add-log="addLog"
        />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import GraphPanel from '../components/GraphPanel.vue'
import Step1GraphBuild from '../components/Step1GraphBuild.vue'
import Step2EnvSetup from '../components/Step2EnvSetup.vue'
import { generateOntologyChunked, regenerateOntology, waitForOntology, getProject, buildGraph, getTaskStatus, getGraphData } from '../api/graph'

const ontologyController = new AbortController()
import { waitForTask } from '../api/taskPolling'
import { getPendingUpload, clearPendingUpload, restorePendingUpload } from '../store/pendingUpload'
import LanguageSwitcher from '../components/LanguageSwitcher.vue'

const route = useRoute()
const router = useRouter()
const { t, tm } = useI18n()

// Layout State
const viewMode = ref('split') // graph | split | workbench

// Step State
const currentStep = ref(1) // 1: 图谱构建, 2: 环境搭建, 3: 开始模拟, 4: 报告生成, 5: 深度互动
const stepNames = computed(() => tm('main.stepNames'))

// Data State
const currentProjectId = ref(route.params.projectId)
const loading = ref(false)
const graphLoading = ref(false)
const error = ref('')
const projectData = ref(null)
const graphData = ref(null)
const currentPhase = ref(-1) // -1: Upload, 0: Ontology, 1: Build, 2: Complete
const ontologyProgress = ref(null)
const buildProgress = ref(null)
const systemLogs = ref([])

// Polling timers
let taskController = null
let graphPollTimer = null
let graphPolling = false
let graphPollingEpoch = 0

// --- Computed Layout Styles ---
const leftPanelStyle = computed(() => {
  if (viewMode.value === 'graph') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'workbench') return { width: '0%', opacity: 0, transform: 'translateX(-20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

const rightPanelStyle = computed(() => {
  if (viewMode.value === 'workbench') return { width: '100%', opacity: 1, transform: 'translateX(0)' }
  if (viewMode.value === 'graph') return { width: '0%', opacity: 0, transform: 'translateX(20px)' }
  return { width: '50%', opacity: 1, transform: 'translateX(0)' }
})

// --- Status Computed ---
const statusClass = computed(() => {
  if (error.value) return 'error'
  if (currentPhase.value >= 2) return 'completed'
  return 'processing'
})

const statusText = computed(() => {
  if (error.value) return 'Error'
  if (currentPhase.value >= 2) return 'Ready'
  if (currentPhase.value === 1) return 'Building Graph'
  if (currentPhase.value === 0) return 'Generating Ontology'
  return 'Initializing'
})

// --- Helpers ---
const addLog = (msg) => {
  const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }) + '.' + new Date().getMilliseconds().toString().padStart(3, '0')
  systemLogs.value.push({ time, msg })
  // Keep last 100 logs
  if (systemLogs.value.length > 100) {
    systemLogs.value.shift()
  }
}

// --- Layout Methods ---
const toggleMaximize = (target) => {
  if (viewMode.value === target) {
    viewMode.value = 'split'
  } else {
    viewMode.value = target
  }
}

const handleNextStep = (params = {}) => {
  if (currentStep.value < 5) {
    currentStep.value++
    addLog(t('log.enterStep', { step: currentStep.value, name: stepNames.value[currentStep.value - 1] }))
    
    // 如果是从 Step 2 进入 Step 3，记录模拟轮数配置
    if (currentStep.value === 3 && params.maxRounds) {
      addLog(t('log.customSimRounds', { rounds: params.maxRounds }))
    }
  }
}

const handleGoBack = () => {
  if (currentStep.value > 1) {
    currentStep.value--
    addLog(t('log.returnToStep', { step: currentStep.value, name: stepNames.value[currentStep.value - 1] }))
  }
}

// --- Data Logic ---

const initProject = async () => {
  addLog('Project view initialized.')
  if (currentProjectId.value === 'new') {
    await handleNewProject()
  } else {
    await loadProject()
  }
}

const handleNewProject = async () => {
  // Try in-memory first, then restore from IndexedDB (survives reloads/HMR)
  let pending = getPendingUpload()
  if (!pending.isPending || pending.files.length === 0) {
    addLog('In-memory store empty — restoring from IndexedDB...')
    await restorePendingUpload()
    pending = getPendingUpload()
  }
  if (!pending.isPending || pending.files.length === 0) {
    error.value = 'No pending files found.'
    addLog('Error: No pending files found for new project.')
    return
  }
  
  try {
    loading.value = true
    currentPhase.value = 0
    ontologyProgress.value = { message: 'Uploading and analyzing docs...' }
    addLog('Starting ontology generation: Uploading files...')
    
    const res = await generateOntologyChunked({
      files: pending.files,
      simulationRequirement: pending.simulationRequirement,
      signal: ontologyController.signal,
      onTaskStarted: ({ project_id }) => {
        currentProjectId.value = project_id
        clearPendingUpload()
        router.replace({ name: 'Process', params: { projectId: project_id } })
      },
      onProgress: (task) => { ontologyProgress.value = { message: task.message || 'Generating ontology...' } },
      onChunkStart: ({ chunkIndex, totalChunks, fileCount }) => {
        if (totalChunks <= 1) {
          addLog('Uploading ontology documents...')
          return
        }
        addLog(`Uploading ontology chunk ${chunkIndex}/${totalChunks} (${fileCount} file(s))...`)
      }
    })
    if (res.success) {
      clearPendingUpload()
      currentProjectId.value = res.data.project_id
      projectData.value = res.data
      
      router.replace({ name: 'Process', params: { projectId: res.data.project_id } })
      ontologyProgress.value = null
      addLog(`Ontology generated successfully for project ${res.data.project_id}`)
      await startBuildGraph()
    } else {
      error.value = res.error || 'Ontology generation failed'
      addLog(`Error generating ontology: ${error.value}`)
    }
  } catch (err) {
    error.value = err.message
    addLog(`Exception in handleNewProject: ${err.message}`)
  } finally {
    loading.value = false
  }
}

const retryProject = async () => {
  try {
    loading.value = true
    error.value = null
    const latest = await getProject(currentProjectId.value, { signal: ontologyController.signal })
    projectData.value = latest.data
    if (latest.data.status === 'failed' && !latest.data.ontology && !latest.data.graph_id) {
      currentPhase.value = 0
      ontologyProgress.value = { message: 'Generating ontology...' }
      const result = await regenerateOntology(currentProjectId.value, {
        signal: ontologyController.signal,
        onProgress: (task) => { ontologyProgress.value = { message: task.message || 'Generating ontology...' } },
      })
      projectData.value = { ...result.data, status: 'ontology_generated' }
      ontologyProgress.value = null
      await startBuildGraph()
    } else if (latest.data.status === 'failed' && latest.data.ontology) {
      await startBuildGraph(true)
    } else {
      await loadProject()
    }
  } catch (err) {
    error.value = err.message || 'Could not recover this project'
  } finally {
    loading.value = false
  }
}

const loadProject = async () => {
  try {
    loading.value = true
    error.value = null
    addLog(`Loading project ${currentProjectId.value}...`)
    let res = await getProject(currentProjectId.value, { signal: ontologyController.signal })
    if (res.success) {
      projectData.value = res.data
      if (res.data.status === 'failed') throw new Error(res.data.error || 'Project generation failed')
      if (res.data.status === 'created' && res.data.ontology_task_id) {
        currentPhase.value = 0
        ontologyProgress.value = { message: 'Generating ontology...' }
        const completed = await waitForOntology(res.data.ontology_task_id, {
          signal: ontologyController.signal,
          onProgress: (task) => { ontologyProgress.value = { message: task.message || 'Generating ontology...' } },
        })
        res = { success: true, data: { ...res.data, ...completed.data, status: 'ontology_generated' } }
        ontologyProgress.value = null
      }
      projectData.value = res.data
      updatePhaseByStatus(res.data.status)
      addLog(`Project loaded. Status: ${res.data.status}`)
      
      if (res.data.status === 'ontology_generated' && !res.data.graph_id) {
        await startBuildGraph()
      } else if (res.data.status === 'graph_building' && res.data.graph_build_task_id) {
        currentPhase.value = 1
        startPollingTask(res.data.graph_build_task_id)
      } else if (res.data.status === 'graph_completed' && res.data.graph_id) {
        currentPhase.value = 2
        await loadGraph(res.data.graph_id)
      }
    } else {
      error.value = res.error
      addLog(`Error loading project: ${res.error}`)
    }
  } catch (err) {
    error.value = err.message
    addLog(`Exception in loadProject: ${err.message}`)
  } finally {
    loading.value = false
  }
}

const updatePhaseByStatus = (status) => {
  switch (status) {
    case 'created':
    case 'ontology_generated': currentPhase.value = 0; break;
    case 'graph_building': currentPhase.value = 1; break;
    case 'graph_completed': currentPhase.value = 2; break;
    case 'failed': error.value = 'Project failed'; break;
  }
}

const startBuildGraph = async (preserveFailedGraph = false) => {
  try {
    currentPhase.value = 1
    if (preserveFailedGraph) {
      graphData.value = null
      projectData.value = { ...projectData.value, graph_id: null, status: 'graph_building' }
    }
    buildProgress.value = { progress: 0, message: 'Starting build...' }
    addLog('Initiating graph build...')
    
    const res = await buildGraph({ project_id: currentProjectId.value, preserve_failed_graph: preserveFailedGraph })
    if (res.success) {
      if (res.data.reused && res.data.graph_id) {
        currentPhase.value = 2
        buildProgress.value = null
        const projectRes = await getProject(currentProjectId.value)
        if (projectRes.success) {
          projectData.value = projectRes.data
        }
        await loadGraph(res.data.graph_id)
        return
      }

      addLog(`Graph build task started. Task ID: ${res.data.task_id}`)
      startPollingTask(res.data.task_id)
    } else {
      error.value = res.error
      addLog(`Error starting build: ${res.error}`)
    }
  } catch (err) {
    error.value = err.message
    addLog(`Exception in startBuildGraph: ${err.message}`)
  }
}

const startGraphPolling = () => {
  stopGraphPolling()
  graphPolling = true
  const epoch = graphPollingEpoch
  const active = () => graphPolling && epoch === graphPollingEpoch
  const refresh = async () => {
    try {
      const project = await getProject(currentProjectId.value, { signal: ontologyController.signal })
      if (!active()) return
      projectData.value = project.data
      if (project.data.graph_id) {
        const graph = await getGraphData(project.data.graph_id, ontologyController.signal)
        if (active() && graph.success) graphData.value = graph.data
      }
    } catch (err) {
      if (!ontologyController.signal.aborted) addLog(`Graph refresh unavailable: ${err.message}`)
    } finally {
      if (active()) graphPollTimer = setTimeout(refresh, 10000)
    }
  }
  void refresh()
}

const startPollingTask = async (taskId) => {
  stopPolling()
  const controller = new AbortController()
  taskController = controller
  startGraphPolling()
  try {
    await waitForTask((signal) => getTaskStatus(taskId, signal), {
      signal: controller.signal,
      onProgress: (task) => {
        if (task.message && task.message !== buildProgress.value?.message) addLog(task.message)
        buildProgress.value = { progress: task.progress || 0, message: task.message }
      },
    })
    stopGraphPolling()
    const project = await getProject(currentProjectId.value, { signal: controller.signal })
    projectData.value = project.data
    if (!project.data.graph_id) throw new Error('Graph build completed without a graph')
    await loadGraph(project.data.graph_id)
    currentPhase.value = 2
    addLog('Graph build task completed.')
  } catch (err) {
    if (!controller.signal.aborted) {
      stopGraphPolling()
      error.value = err.message
      addLog(`Graph build task failed: ${err.message}`)
    }
  } finally {
    if (taskController === controller) taskController = null
  }
}

const loadGraph = async (graphId) => {
  graphLoading.value = true
  addLog(`Loading full graph data: ${graphId}`)
  try {
    const res = await getGraphData(graphId)
    if (res.success) {
      graphData.value = res.data
      addLog('Graph data loaded successfully.')
    } else {
      throw new Error(res.error || 'Graph data is unavailable')
    }
  } catch (e) {
    addLog(`Exception loading graph: ${e.message}`)
    throw e
  } finally {
    graphLoading.value = false
  }
}

const refreshGraph = async () => {
  if (projectData.value?.graph_id) {
    try {
      error.value = ''
      addLog('Manual graph refresh triggered.')
      await loadGraph(projectData.value.graph_id)
    } catch (err) {
      error.value = err.message
    }
  }
}

const stopPolling = () => {
  taskController?.abort()
  taskController = null
}

const stopGraphPolling = () => {
  graphPolling = false
  graphPollingEpoch++
  if (graphPollTimer) clearTimeout(graphPollTimer)
  graphPollTimer = null
}

onMounted(() => {
  initProject()
})

onUnmounted(() => {
  ontologyController.abort()
  stopPolling()
  stopGraphPolling()
})
</script>

<style scoped>
.generation-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.75rem 1.5rem;
  color: #9a3412;
  background: #fff7ed;
  border-bottom: 1px solid #fdba74;
}
.generation-error button {
  padding: 0.4rem 0.8rem;
  border: 1px solid currentColor;
  border-radius: 4px;
  background: white;
  cursor: pointer;
}
.main-view {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #FFF;
  overflow: hidden;
  font-family: 'Space Grotesk', 'Noto Sans SC', system-ui, sans-serif;
}

/* Header */
.app-header {
  height: 60px;
  border-bottom: 1px solid #EAEAEA;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: #FFF;
  z-index: 100;
  position: relative;
}

.header-center {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}

.brand {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 18px;
  letter-spacing: 1px;
  cursor: pointer;
}

.view-switcher {
  display: flex;
  background: #F5F5F5;
  padding: 4px;
  border-radius: 6px;
  gap: 4px;
}

.switch-btn {
  border: none;
  background: transparent;
  padding: 6px 16px;
  font-size: 12px;
  font-weight: 600;
  color: #666;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.switch-btn.active {
  background: #FFF;
  color: #000;
  box-shadow: 0 2px 4px rgba(0,0,0,0.05);
}

.status-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #666;
  font-weight: 500;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.workflow-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.step-num {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  color: #999;
}

.step-name {
  font-weight: 700;
  color: #000;
}

.step-divider {
  width: 1px;
  height: 14px;
  background-color: #E0E0E0;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #CCC;
}

.status-indicator.processing .dot { background: #FF5722; animation: pulse 1s infinite; }
.status-indicator.completed .dot { background: #4CAF50; }
.status-indicator.error .dot { background: #F44336; }

@keyframes pulse { 50% { opacity: 0.5; } }

/* Content */
.content-area {
  flex: 1;
  display: flex;
  position: relative;
  overflow: hidden;
}

.panel-wrapper {
  height: 100%;
  overflow: hidden;
  transition: width 0.4s cubic-bezier(0.25, 0.8, 0.25, 1), opacity 0.3s ease, transform 0.3s ease;
  will-change: width, opacity, transform;
}

.panel-wrapper.left {
  border-right: 1px solid #EAEAEA;
}
</style>
