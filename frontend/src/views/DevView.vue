<script setup>
// Developer tools from the Phase 0 console: dummy jobs that exercise every job state,
// all jobs, and the raw SSE event log.
import { inject, onMounted, reactive } from 'vue'
import { api } from '../api.js'
import { TERMINAL, refreshJobs, upsertJob, useJobs, watchJob } from '../composables/useJobs.js'

const token = inject('token')
const report = inject('report')
const { state } = useJobs()
const STAGES = ['probe', 'asr', 'ocr', 'extract', 'resolve', 'aggregate', 'index']
const options = reactive({ step_seconds: 1, fail_at: '', needs_review: false })
const health = reactive({ text: 'checking…' })

async function createJob() {
  try {
    const job = await api.createDummyJob(token.value, {
      step_seconds: Number(options.step_seconds),
      fail_at: options.fail_at || null,
      needs_review: options.needs_review,
    })
    upsertJob(job)
    watchJob(token.value, job.id)
  } catch (err) {
    report(err)
  }
}

async function act(fn, jobId) {
  try {
    const job = await fn(token.value, jobId)
    upsertJob(job)
    if (!TERMINAL.has(job.status)) watchJob(token.value, job.id)
  } catch (err) {
    report(err)
  }
}

onMounted(async () => {
  try {
    const result = await api.health()
    health.text = `api ${result.status}, database ${result.database}`
  } catch (err) {
    health.text = `unreachable (${err.message})`
  }
  try {
    await refreshJobs(token.value)
  } catch (err) {
    report(err)
  }
})
</script>

<template>
  <p class="muted">Backend: {{ health.text }}</p>
  <section class="card">
    <h2>Start a dummy job</h2>
    <form class="row wrap" @submit.prevent="createJob">
      <label>Seconds per stage <input v-model="options.step_seconds" type="number" min="0" max="30" step="0.5" /></label>
      <label>Fail at
        <select v-model="options.fail_at">
          <option value="">never</option>
          <option v-for="s in STAGES" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="inline"><input v-model="options.needs_review" type="checkbox" /> End in needs_review</label>
      <button type="submit">Start</button>
    </form>
  </section>

  <section class="card">
    <h2>Jobs</h2>
    <p v-if="!state.jobs.length" class="muted">No jobs yet.</p>
    <div v-else class="table-wrap">
      <table>
        <thead><tr><th>Job</th><th>Kind</th><th>Status</th><th>Stage</th><th>Progress</th><th>Error</th><th></th></tr></thead>
        <tbody>
          <tr v-for="job in state.jobs" :key="job.id">
            <td><code>{{ job.id.slice(0, 8) }}</code></td>
            <td>{{ job.kind }}</td>
            <td><span :class="['badge', job.status]">{{ job.status }}</span></td>
            <td>{{ job.stage ?? '–' }}</td>
            <td><progress max="100" :value="job.progress"></progress> {{ job.progress }}%</td>
            <td class="muted small-text">{{ job.error ?? '' }}</td>
            <td>
              <button v-if="job.status === 'failed'" class="small" @click="act(api.retryJob, job.id)">Retry</button>
              <button v-if="job.status === 'needs_review' && job.kind === 'dummy'" class="small" @click="act(api.approveJob, job.id)">Approve</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h2>Event log <small class="muted">(SSE)</small></h2>
    <pre>{{ state.log.join('\n') || 'Waiting for events…' }}</pre>
  </section>
</template>
