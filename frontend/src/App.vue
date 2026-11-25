<script setup>
// Test console: auth, videos + jobs (Phase 0/1), places (1), Q&A (2).
import { onBeforeUnmount, onMounted, provide, reactive, ref, watch as watchValue } from 'vue'
import { api, loadToken, saveToken } from './api.js'
import AskPanel from './components/AskPanel.vue'
import PlacesPanel from './components/PlacesPanel.vue'

const STAGES = ['probe', 'asr', 'ocr', 'extract', 'resolve', 'aggregate', 'index']
const TERMINAL = new Set(['done', 'failed'])
const TABS = ['videos', 'places', 'ask']

function storedTab() {
  try {
    return TABS.includes(localStorage.getItem('travelrag.tab')) ? localStorage.getItem('travelrag.tab') : 'videos'
  } catch {
    return 'videos'
  }
}
const tab = ref(storedTab())
watchValue(tab, (value) => {
  try {
    localStorage.setItem('travelrag.tab', value)
  } catch {
    /* ignore */
  }
})
const askScope = ref(null) // {id, name} when asking about one place
const clip = ref(null) // {videoId, startMs, title} shown in the player overlay

const token = ref(loadToken())
const user = ref(null)
const health = ref('checking…')
const error = ref('')

const credentials = reactive({ email: '', password: '' })
const options = reactive({ step_seconds: 1, fail_at: '', needs_review: false })

const jobs = ref([])
const videos = ref([])
const upload = reactive({ file: null, progress: 0, busy: false })
const playing = ref(null) // id of the video shown in the player
const log = ref([])
const streams = new Map() // job id -> EventSource

function report(err) {
  error.value = err?.message ?? String(err)
}

function playClip(videoId, startMs, title) {
  clip.value = { videoId, startMs: startMs ?? 0, title }
}

provide('token', token)
provide('report', report)
provide('playClip', playClip)
provide('askAbout', (place) => {
  askScope.value = { id: place.id, name: place.name }
  tab.value = 'ask'
})

function addLog(line) {
  log.value.unshift(`${new Date().toLocaleTimeString()}  ${line}`)
  log.value.length = Math.min(log.value.length, 200)
}

function upsertJob(patch) {
  const index = jobs.value.findIndex((j) => j.id === patch.id)
  if (index === -1) jobs.value.unshift(patch)
  else jobs.value[index] = { ...jobs.value[index], ...patch }
  const video = videos.value.find((v) => v.latest_job?.id === patch.id)
  if (video) {
    video.latest_job = { ...video.latest_job, ...patch }
    // Metadata (duration, size) is written by the probe stage; reload once the job settles.
    if (TERMINAL.has(patch.status)) refreshVideos()
  }
}

async function refreshVideos() {
  try {
    videos.value = await api.listVideos(token.value)
  } catch (err) {
    report(err)
  }
}

async function uploadSelected() {
  if (!upload.file) return
  error.value = ''
  upload.busy = true
  upload.progress = 0
  try {
    const video = await api.uploadVideo(token.value, upload.file, (p) => (upload.progress = p))
    videos.value.unshift(video)
    upsertJob(video.latest_job)
    watch(video.latest_job.id)
    upload.file = null
    document.getElementById('video-input').value = ''
  } catch (err) {
    report(err)
  } finally {
    upload.busy = false
  }
}

async function removeVideo(video) {
  if (!confirm(`Delete "${video.original_name}" and its jobs?`)) return
  try {
    await api.deleteVideo(token.value, video.id)
    if (video.latest_job) unwatch(video.latest_job.id)
    videos.value = videos.value.filter((v) => v.id !== video.id)
    jobs.value = jobs.value.filter((j) => j.video_id !== video.id)
    if (playing.value === video.id) playing.value = null
  } catch (err) {
    report(err)
  }
}

function formatBytes(n) {
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function formatDuration(ms) {
  if (ms == null) return '–'
  const s = Math.round(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
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
    await refreshVideos()
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
  videos.value = []
  playing.value = null
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

      <nav class="tabs">
        <button v-for="t in TABS" :key="t" :class="{ active: tab === t }" @click="tab = t">{{ t }}</button>
      </nav>

      <PlacesPanel v-if="tab === 'places'" />
      <AskPanel v-else-if="tab === 'ask'" :scope="askScope" @clear-scope="askScope = null" />

      <template v-if="tab === 'videos'">
      <section class="card">
        <h2>Videos</h2>
        <form class="row wrap" @submit.prevent="uploadSelected">
          <input
            id="video-input"
            type="file"
            accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.m4v,.webm,.mkv"
            @change="upload.file = $event.target.files[0] ?? null"
          />
          <button type="submit" :disabled="!upload.file || upload.busy">
            {{ upload.busy ? 'Uploading…' : 'Upload' }}
          </button>
          <span v-if="upload.busy"><progress max="100" :value="upload.progress"></progress> {{ upload.progress }}%</span>
        </form>

        <p v-if="!videos.length" class="muted">No videos yet.</p>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr><th>Name</th><th>Size</th><th>Length</th><th>Resolution</th><th>Audio</th><th>Processing</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="video in videos" :key="video.id">
                <td class="name" :title="video.original_name">{{ video.original_name }}</td>
                <td>{{ formatBytes(video.size_bytes) }}</td>
                <td>{{ formatDuration(video.duration_ms) }}</td>
                <td>{{ video.width ? `${video.width}×${video.height}` : '–' }}</td>
                <td>{{ video.has_audio == null ? '–' : video.has_audio ? 'yes' : 'no' }}</td>
                <td>
                  <template v-if="video.latest_job">
                    <span :class="['badge', video.latest_job.status]">{{ video.latest_job.status }}</span>
                    <span class="muted"> {{ video.latest_job.stage ?? '' }}</span>
                    <div v-if="video.latest_job.error" class="error small-text">{{ video.latest_job.error }}</div>
                  </template>
                </td>
                <td class="actions">
                  <button class="small secondary" @click="playing = playing === video.id ? null : video.id">
                    {{ playing === video.id ? 'Hide' : 'Play' }}
                  </button>
                  <button v-if="video.latest_job?.status === 'failed'" class="small" @click="act(api.retryJob, video.latest_job.id)">Retry</button>
                  <button class="small danger" @click="removeVideo(video)">Delete</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <video
          v-if="playing"
          :key="playing"
          class="player"
          :src="api.videoFileUrl(token, playing)"
          controls
          autoplay
        ></video>
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
    </template>

    <div v-if="clip" class="overlay" @click.self="clip = null">
      <div class="card player-card">
        <div class="row spread">
          <strong>{{ clip.title }}</strong>
          <button class="small secondary" @click="clip = null">Close</button>
        </div>
        <video
          :key="`${clip.videoId}-${clip.startMs}`"
          class="player"
          :src="`${api.videoFileUrl(token, clip.videoId)}#t=${Math.floor(clip.startMs / 1000)}`"
          controls
          autoplay
        ></video>
        <p class="muted small-text">Demo-seeded videos have no file, so they cannot play.</p>
      </div>
    </div>
  </main>
</template>
