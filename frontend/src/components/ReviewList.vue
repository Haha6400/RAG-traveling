<script setup>
// "Is this X?" questions from place resolution.
import { inject } from 'vue'
import { api } from '../api.js'
import { timestamp } from '../lib/format.js'

defineProps({ reviews: { type: Array, required: true } })
const emit = defineEmits(['changed'])
const token = inject('token')
const report = inject('report')
const playClip = inject('playClip')

async function run(fn) {
  try {
    await fn()
    emit('changed')
  } catch (err) {
    report(err)
  }
}
</script>

<template>
  <div class="reviews">
    <div v-for="r in reviews" :key="r.mention_id" class="review">
      <p>
        A video mentions <strong>{{ r.name }}</strong>
        <span class="muted">{{ r.area ? `(${r.area})` : '' }} at {{ timestamp(r.start_ms) }}</span>. Is it one of these?
        <button class="small secondary" @click="playClip(r.video_id, r.start_ms, r.name)">▶</button>
      </p>
      <ul>
        <li v-for="c in r.candidates" :key="c.id">
          <template v-if="c.place">{{ c.place.name }} <span class="muted">{{ c.place.area || c.place.address }}</span></template>
          <template v-else>{{ c.osm.name }} <span class="muted">(OpenStreetMap) {{ c.osm.address }}</span></template>
          <button class="small" @click="run(() => api.acceptCandidate(token, c.id))">Yes</button>
        </li>
      </ul>
      <button class="small secondary" @click="run(() => api.rejectMention(token, r.mention_id))">None — new place</button>
    </div>
  </div>
</template>
