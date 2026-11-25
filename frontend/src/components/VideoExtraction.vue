<script setup>
// What the LLM concluded from one video: every field it filled per place (null fields shown
// as "not stated", so gaps are visible), where each place ended up, and the transcript + OCR
// text it read, to see why a field is wrong or missing.
import { inject, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { CATEGORY_COLORS, timestamp } from '../lib/format.js'

const props = defineProps({ video: { type: Object, required: true } })
const token = inject('token')
const report = inject('report')
const playClip = inject('playClip')

const result = ref(null)
const chunks = ref([])
const loading = ref(true)

const STATUS = {
  resolved: 'on your map',
  review: 'waiting for you to confirm the match',
  pending: 'not matched yet',
}

function price(d) {
  if (d.price_min == null && d.price_max == null) return null
  const n = (v) => (v == null ? '?' : v.toLocaleString('vi-VN'))
  const range = d.price_min === d.price_max ? n(d.price_min) : `${n(d.price_min)}–${n(d.price_max)}`
  return [range, d.currency, d.price_unit && `(${d.price_unit})`].filter(Boolean).join(' ')
}

function hours(d) {
  if (!d.opening_hours_text && !d.opening_hours_osm) return null
  return [d.opening_hours_text, d.opening_hours_osm && `→ ${d.opening_hours_osm}`].filter(Boolean).join(' ')
}

function fields(d) {
  return [
    ['Category', d.category],
    ['Area', d.area],
    ['Address', d.address_hint],
    ['Price', price(d)],
    ['Hours', hours(d)],
    ['Best time', d.best_time],
    ['Strengths', d.pros?.length ? d.pros.join('; ') : null],
    ['Weaknesses', d.cons?.length ? d.cons.join('; ') : null],
    ['Recommendation', d.recommendation == null ? null : `${d.recommendation}/5`],
  ]
}

onMounted(async () => {
  try {
    ;[result.value, chunks.value] = await Promise.all([
      api.videoExtraction(token.value, props.video.id),
      api.transcript(token.value, props.video.id),
    ])
  } catch (err) {
    report(err)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="extraction">
    <p v-if="loading" class="muted">Loading…</p>
    <template v-else-if="result">
      <p v-if="!result.extracted_at" class="muted">
        The LLM has not read this video yet<template v-if="video.latest_job?.stage"> (now at: {{ video.latest_job.stage }})</template>.
      </p>
      <template v-else>
        <p class="muted small-text wide">
          Extracted {{ new Date(result.extracted_at).toLocaleString() }}
          <template v-if="result.language"> · speech language: {{ result.language }}</template>
          <template v-if="result.mentions[0]?.details.prompt_version"> · prompt {{ result.mentions[0].details.prompt_version }}</template>
        </p>
        <p v-if="!result.mentions.length" class="muted">The LLM found no places in this video.</p>

        <div v-for="m in result.mentions" :key="m.id" class="mention-card">
          <div class="row spread">
            <strong><span class="dot" :style="{ background: CATEGORY_COLORS[m.details.category] ?? CATEGORY_COLORS.other }"></span>{{ m.name }}</strong>
            <button class="small secondary" @click="playClip(video.id, m.start_ms, `${video.original_name} · ${m.name}`)">
              ▶ {{ timestamp(m.start_ms) }}–{{ timestamp(m.end_ms) }}
            </button>
          </div>
          <p class="small-text wide">
            <span :class="['badge', m.status === 'resolved' ? 'done' : 'needs_review']">{{ STATUS[m.status] ?? m.status }}</span>
            <RouterLink v-if="m.place_id" :to="{ path: '/', query: { place: m.place_id } }">
              {{ m.place_name && m.place_name !== m.name ? `as “${m.place_name}”` : 'open on map' }}
            </RouterLink>
            <RouterLink v-else-if="m.status === 'review'" to="/">confirm on the map</RouterLink>
            <span class="muted"> · LLM confidence {{ Math.round(m.confidence * 100) }}%</span>
          </p>
          <dl>
            <template v-for="[label, value] in fields(m.details)" :key="label">
              <dt>{{ label }}</dt>
              <dd :class="{ muted: value == null }">{{ value ?? 'not stated' }}</dd>
            </template>
          </dl>
        </div>
      </template>

      <details v-if="chunks.length">
        <summary>What the LLM read ({{ chunks.length }} {{ chunks.length === 1 ? 'chunk' : 'chunks' }}: speech + on-screen text)</summary>
        <div v-for="c in chunks" :key="c.seq" class="chunk">
          <div class="muted small-text">[{{ c.seq }}] {{ timestamp(c.start_ms) }}–{{ timestamp(c.end_ms) }}</div>
          <div>{{ c.text || '(no speech)' }}</div>
          <div v-if="c.ocr_text" class="muted small-text wide">On screen: {{ c.ocr_text }}</div>
        </div>
      </details>
    </template>
  </div>
</template>
