<script setup>
// Overlay that plays a video from a given timestamp (media fragment #t=seconds).
import { onBeforeUnmount, onMounted } from 'vue'
import { api } from '../api.js'

const props = defineProps({ clip: { type: Object, required: true }, token: { type: String, required: true } })
const emit = defineEmits(['close'])

function onKey(event) {
  if (event.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="card player-card">
      <div class="row spread">
        <strong>{{ props.clip.title }}</strong>
        <button class="small secondary" @click="emit('close')">Close</button>
      </div>
      <video
        :key="`${props.clip.videoId}-${props.clip.startMs}`"
        class="player"
        :src="`${api.videoFileUrl(props.token, props.clip.videoId)}#t=${Math.floor(props.clip.startMs / 1000)}`"
        controls
        autoplay
      ></video>
      <p class="muted small-text">Demo-seeded videos have no file, so they cannot play.</p>
    </div>
  </div>
</template>
