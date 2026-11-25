<script setup>
// Upload videos and follow their processing jobs live.
import { inject, onMounted, reactive, ref } from 'vue'
import { api } from '../api.js'
import { TERMINAL, refreshJobs, upsertJob, useJobs, watchJob } from '../composables/useJobs.js'
import { bytes, duration } from '../lib/format.js'
import VideoExtraction from '../components/VideoExtraction.vue'

const token = inject('token')
const report = inject('report')
const playClip = inject('playClip')

const videos = ref([])
const upload = reactive({ file: null, progress: 0, busy: false })
const fileInput = ref(null)
const openExtraction = ref(null) // id of the video whose LLM result is shown

async function refreshVideos() {
  try {
    videos.value = await api.listVideos(token.value)
  } catch (err) {
    report(err)
  }
}

// Keep each video's job badge in sync with the live stream; reload when a job settles
// (the probe stage writes duration/size, the pipeline may leave reviews behind).
useJobs((patch) => {
  const video = videos.value.find((v) => v.latest_job?.id === patch.id)
  if (!video) return
  video.latest_job = { ...video.latest_job, ...patch }
  if (TERMINAL.has(patch.status) || patch.status === 'needs_review') refreshVideos()
})

async function uploadSelected() {
  if (!upload.file) return
  upload.busy = true
  upload.progress = 0
  try {
    const video = await api.uploadVideo(token.value, upload.file, (p) => (upload.progress = p))
    videos.value.unshift(video)
    upsertJob(video.latest_job)
    watchJob(token.value, video.latest_job.id)
    upload.file = null
    fileInput.value.value = ''
  } catch (err) {
    report(err)
  } finally {
    upload.busy = false
  }
}

async function retry(video) {
  try {
    const job = await api.retryJob(token.value, video.latest_job.id)
    upsertJob(job)
    video.latest_job = job
    watchJob(token.value, job.id)
  } catch (err) {
    report(err)
  }
}

async function remove(video) {
  if (!confirm(`Delete "${video.original_name}"? Places only this video mentions are removed from your map.`)) return
  try {
    await api.deleteVideo(token.value, video.id)
    videos.value = videos.value.filter((v) => v.id !== video.id)
  } catch (err) {
    report(err)
  }
}

onMounted(async () => {
  await refreshVideos()
  try {
    await refreshJobs(token.value)
  } catch (err) {
    report(err)
  }
})
</script>

<template>
  <section class="card">
    <h2>Upload a review video</h2>
    <form class="row wrap" @submit.prevent="uploadSelected">
      <input
        ref="fileInput"
        type="file"
        accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.m4v,.webm,.mkv"
        @change="upload.file = $event.target.files[0] ?? null"
      />
      <button type="submit" :disabled="!upload.file || upload.busy">{{ upload.busy ? 'Uploading…' : 'Upload' }}</button>
      <span v-if="upload.busy"><progress max="100" :value="upload.progress"></progress> {{ upload.progress }}%</span>
    </form>
    <p class="muted small-text">.mp4 .mov .m4v .webm .mkv · up to 500 MB and 20 min. Places found in the video appear on your map.</p>
  </section>

  <section class="card">
    <h2>Your videos</h2>
    <p v-if="!videos.length" class="muted">No videos yet.</p>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr><th>Name</th><th>Size</th><th>Length</th><th>Processing</th><th></th></tr>
        </thead>
        <tbody>
          <template v-for="video in videos" :key="video.id">
            <tr>
              <td class="name" :title="video.original_name">{{ video.original_name }}</td>
              <td>{{ bytes(video.size_bytes) }}</td>
              <td>{{ duration(video.duration_ms) }}</td>
              <td>
                <template v-if="video.latest_job">
                  <span :class="['badge', video.latest_job.status]">{{ video.latest_job.status.replace('_', ' ') }}</span>
                  <span v-if="!TERMINAL.has(video.latest_job.status)" class="muted"> {{ video.latest_job.stage ?? '' }} {{ video.latest_job.progress }}%</span>
                  <RouterLink v-if="video.latest_job.status === 'needs_review'" to="/" class="small-text"> confirm places on the map</RouterLink>
                  <div v-if="video.latest_job.error" class="error small-text">{{ video.latest_job.error }}</div>
                </template>
              </td>
              <td class="actions">
                <button class="small secondary" @click="playClip(video.id, 0, video.original_name)">Play</button>
                <button
                  class="small secondary"
                  :aria-expanded="openExtraction === video.id"
                  @click="openExtraction = openExtraction === video.id ? null : video.id"
                >
                  {{ openExtraction === video.id ? 'Hide result' : 'LLM result' }}
                </button>
                <button v-if="video.latest_job?.status === 'failed'" class="small" @click="retry(video)">Retry</button>
                <button class="small danger" @click="remove(video)">Delete</button>
              </td>
            </tr>
            <tr v-if="openExtraction === video.id" class="extraction-row">
              <td colspan="5"><VideoExtraction :video="video" :key="video.latest_job?.status" /></td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>
