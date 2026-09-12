/**
 * Pending Upload Store — persists files + requirement in IndexedDB
 * so they survive page reloads and HMR cycles.
 *
 * File objects cannot be stored in sessionStorage/localStorage,
 * but IndexedDB handles Blobs/Files natively.
 */
import { reactive } from 'vue'

const DB_NAME = 'mirofish_pending'
const STORE_NAME = 'uploads'
const KEY = 'pending'

// ---------------------------------------------------------------------------
// In-memory reactive state (Vue reads from here)
// ---------------------------------------------------------------------------
const state = reactive({
  files: [],
  simulationRequirement: '',
  isPending: false
})

// ---------------------------------------------------------------------------
// IndexedDB helpers
// ---------------------------------------------------------------------------

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME)
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

async function saveToDB(files, requirement) {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)

    // Store file data as {name, type, blob} so we can reconstruct File objects
    const fileEntries = files.map(f => ({
      name: f.name,
      type: f.type,
      lastModified: f.lastModified,
      blob: f          // IndexedDB can store File/Blob directly
    }))

    store.put({ files: fileEntries, requirement, isPending: true }, KEY)
    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve
      tx.onerror = () => reject(tx.error)
    })
    db.close()
  } catch (err) {
    console.warn('[pendingUpload] Failed to save to IndexedDB:', err)
  }
}

async function loadFromDB() {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readonly')
    const store = tx.objectStore(STORE_NAME)
    const req = store.get(KEY)

    const record = await new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => reject(req.error)
    })
    db.close()

    if (!record || !record.isPending) return null

    // Reconstruct File objects from stored blobs
    const files = (record.files || []).map(entry => {
      if (entry.blob instanceof File) return entry.blob
      return new File([entry.blob], entry.name, {
        type: entry.type,
        lastModified: entry.lastModified
      })
    })

    return {
      files,
      simulationRequirement: record.requirement || '',
      isPending: true
    }
  } catch (err) {
    console.warn('[pendingUpload] Failed to load from IndexedDB:', err)
    return null
  }
}

async function clearDB() {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    tx.objectStore(STORE_NAME).delete(KEY)
    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve
      tx.onerror = () => reject(tx.error)
    })
    db.close()
  } catch (err) {
    console.warn('[pendingUpload] Failed to clear IndexedDB:', err)
  }
}

// ---------------------------------------------------------------------------
// Public API (same signatures as before — drop-in replacement)
// ---------------------------------------------------------------------------

export function setPendingUpload(files, requirement) {
  state.files = files
  state.simulationRequirement = requirement
  state.isPending = true
  // Persist in background (fire-and-forget)
  saveToDB(files, requirement)
}

export function getPendingUpload() {
  return {
    files: state.files,
    simulationRequirement: state.simulationRequirement,
    isPending: state.isPending
  }
}

export function clearPendingUpload() {
  state.files = []
  state.simulationRequirement = ''
  state.isPending = false
  clearDB()
}

/**
 * Restore state from IndexedDB on app startup.
 * Call this once (e.g. in main.js or the router guard).
 * Returns true if pending data was restored.
 */
export async function restorePendingUpload() {
  const saved = await loadFromDB()
  if (saved && saved.isPending && saved.files.length > 0) {
    state.files = saved.files
    state.simulationRequirement = saved.simulationRequirement
    state.isPending = true
    console.info(`[pendingUpload] Restored ${saved.files.length} file(s) from IndexedDB`)
    return true
  }
  return false
}

export default state
