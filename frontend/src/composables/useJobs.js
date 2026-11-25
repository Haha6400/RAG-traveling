// Live job progress over SSE, shared by the Videos and Dev views (one stream per job).
import { onBeforeUnmount, reactive } from 'vue'
import { api } from '../api.js'

export const TERMINAL = new Set(['done', 'failed'])

const state = reactive({ jobs: [], log: [] })
const streams = new Map() // job id -> EventSource
const listeners = new Set()

function addLog(line) {
  state.log.unshift(`${new Date().toLocaleTimeString()}  ${line}`)
  state.log.length = Math.min(state.log.length, 200)
}

export function upsertJob(patch) {
  const index = state.jobs.findIndex((j) => j.id === patch.id)
  if (index === -1) state.jobs.unshift(patch)
  else state.jobs[index] = { ...state.jobs[index], ...patch }
  listeners.forEach((fn) => fn(patch))
}

export function watchJob(token, jobId) {
  if (streams.has(jobId)) return
  const source = api.jobEvents(token, jobId)
  streams.set(jobId, source)
  source.addEventListener('job', (message) => {
    const event = JSON.parse(message.data)
    upsertJob({ id: event.job_id, ...event })
    addLog(`${event.job_id.slice(0, 8)}  ${event.status}  ${event.stage ?? '-'}  ${event.progress}%`)
    if (TERMINAL.has(event.status)) unwatchJob(jobId)
  })
  source.onerror = () => {
    // EventSource reconnects by itself; stop once the job is finished.
    const job = state.jobs.find((j) => j.id === jobId)
    if (job && TERMINAL.has(job.status)) unwatchJob(jobId)
  }
}

export function unwatchJob(jobId) {
  streams.get(jobId)?.close()
  streams.delete(jobId)
}

export function closeAllJobStreams() {
  streams.forEach((s) => s.close())
  streams.clear()
  state.jobs = []
  state.log = []
}

export async function refreshJobs(token) {
  state.jobs = await api.listJobs(token)
  state.jobs.filter((j) => !TERMINAL.has(j.status)).forEach((j) => watchJob(token, j.id))
}

/** Use inside a component's setup: `fn(jobPatch)` runs on every job update. */
export function useJobs(onUpdate) {
  if (onUpdate) {
    listeners.add(onUpdate)
    onBeforeUnmount(() => listeners.delete(onUpdate))
  }
  return { state }
}
