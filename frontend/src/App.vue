<script setup>
// Phase 0 test console: auth + dummy jobs + live progress over SSE.
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, loadToken, saveToken } from './api.js'

const STAGES = ['asr', 'ocr', 'extract', 'resolve', 'index']
const TERMINAL = new Set(['done', 'failed'])

const token = ref(loadToken())
const user = ref(null)
const health = ref('checking…')
const error = ref('')

const credentials = reactive({ email: '', password: '' })
const options = reactive({ step_seconds: 1, fail_at: '', needs_review: false })

const jobs = ref([])
const log = ref([])
const streams = new Map() // job id -> EventSource

function report(err) {
  error.value = err?.message ?? String(err)
}

function addLog(line) {
  log.value.unshift(`${new Date().toLocaleTimeString()}  ${line}`)
  log.value.length = Math.min(log.value.length, 200)
}

function upsertJob(patch) {
  const index = jobs.value.findIndex((j) => j.id === patch.id)
  if (index === -1) jobs.value.unshift(patch)
  else jobs.value[index] = { ...jobs.value[index], ...patch }
}

function watch(jobId) {
  if (streams.has(jobId)) return
  const source = api.jobEvents(token.value, jobId)
  streams.set(jobId, source)
  source.addEventListener('job', (message) => {
    const event = JSON.parse(message.data)
    upsertJob({ id: event.job_id, ...event })
    addLog(`${event.job_id.slice(0, 8)}  ${event.status}  ${event.stage ?? '-'}  ${event.progress}%`)
    if (TERMINAL.has(event.status)) unwatch(jobId)
  })
  source.onerror = () => {
    // EventSource reconnects by itself; stop if the job is already finished.
    const job = jobs.value.find((j) => j.id === jobId)
    if (job && TERMINAL.has(job.status)) unwatch(jobId)
  }
}

function unwatch(jobId) {
  streams.get(jobId)?.close()
  streams.delete(jobId)
}

async function refreshSession() {
  if (!token.value) return
  try {
    user.value = await api.me(token.value)
    jobs.value = await api.listJobs(token.value)
    jobs.value.filter((j) => !TERMINAL.has(j.status)).forEach((j) => watch(j.id))
  } catch (err) {
    if (err.status === 401) logout()
    else report(err)
  }
}

async function register() {
  error.value = ''
  try {
    await api.register(credentials.email, credentials.password)
    await login()
  } catch (err) {
    report(err)
  }
}

async function login() {
  error.value = ''
  try {
    const { access_token } = await api.login(credentials.email, credentials.password)
    token.value = access_token
    saveToken(access_token)
    await refreshSession()
  } catch (err) {
    report(err)
  }
}

function logout() {
  streams.forEach((s) => s.close())
  streams.clear()
  token.value = null
  user.value = null
  jobs.value = []
  saveToken(null)
}

async function createJob() {
  error.value = ''
  try {
    const job = await api.createDummyJob(token.value, {
      step_seconds: Number(options.step_seconds),
      fail_at: options.fail_at || null,
      needs_review: options.needs_review,
    })
    upsertJob(job)
    watch(job.id)
  } catch (err) {
    report(err)
  }
}

async function act(fn, jobId) {
  error.value = ''
  try {
    const job = await fn(token.value, jobId)
    upsertJob(job)
    if (!TERMINAL.has(job.status)) watch(job.id)
  } catch (err) {
    report(err)
  }
}

onMounted(async () => {
  try {
    const result = await api.health()
    health.value = `api ${result.status}, database ${result.database}`
  } catch (err) {
    health.value = `unreachable (${err.message})`
  }
  await refreshSession()
})

onBeforeUnmount(() => streams.forEach((s) => s.close()))
</script>

<template>
  <main>
    <header>
      <h1>RAG-traveling <small>Phase 0 console</small></h1>
      <span class="muted">Backend: {{ health }}</span>
    </header>

    <p v-if="error" class="error">{{ error }}</p>

    <section v-if="!user" class="card">
      <h2>Sign in</h2>
      <form @submit.prevent="login">
        <input v-model="credentials.email" type="email" placeholder="email" required />
        <input v-model="credentials.password" type="password" placeholder="password (8+ chars)" minlength="8" required />
        <div class="row">
          <button type="submit">Log in</button>
          <button type="button" class="secondary" @click="register">Register</button>
        </div>
      </form>
    </section>

    <template v-else>
      <section class="card row spread">
        <span>Signed in as <strong>{{ user.email }}</strong></span>
        <button class="secondary" @click="logout">Log out</button>
      </section>

      <section class="card">
        <h2>Start a dummy job</h2>
        <form class="row wrap" @submit.prevent="createJob">
          <label>Seconds per stage
            <input v-model="options.step_seconds" type="number" min="0" max="30" step="0.5" />
          </label>
          <label>Fail at
            <select v-model="options.fail_at">
              <option value="">never</option>
              <option v-for="s in STAGES" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <label class="inline">
            <input v-model="options.needs_review" type="checkbox" /> End in needs_review
          </label>
          <button type="submit">Start</button>
        </form>
      </section>

      <section class="card">
        <h2>Jobs</h2>
        <p v-if="!jobs.length" class="muted">No jobs yet.</p>
        <div class="table-wrap" v-else>
          <table>
            <thead>
              <tr><th>Job</th><th>Status</th><th>Stage</th><th>Progress</th><th>Error</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="job in jobs" :key="job.id">
                <td><code>{{ job.id.slice(0, 8) }}</code></td>
                <td><span :class="['badge', job.status]">{{ job.status }}</span></td>
                <td>{{ job.stage ?? '–' }}</td>
                <td><progress max="100" :value="job.progress"></progress> {{ job.progress }}%</td>
                <td class="muted">{{ job.error ?? '' }}</td>
                <td>
                  <button v-if="job.status === 'failed'" class="small" @click="act(api.retryJob, job.id)">Retry</button>
                  <button v-if="job.status === 'needs_review'" class="small" @click="act(api.approveJob, job.id)">Approve</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <h2>Event log <small class="muted">(SSE)</small></h2>
        <pre>{{ log.join('\n') || 'Waiting for events…' }}</pre>
      </section>
    </template>
  </main>
</template>
