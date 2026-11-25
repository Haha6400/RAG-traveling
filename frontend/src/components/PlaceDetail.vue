<script setup>
// The place card: aggregated facts, conflicts, LLM summary, and the videos it comes from.
import { inject, ref } from 'vue'
import { api } from '../api.js'
import { timestamp } from '../lib/format.js'

const props = defineProps({ detail: { type: Object, required: true } })
const emit = defineEmits(['close', 'ask', 'pin', 'updated'])
const token = inject('token')
const report = inject('report')
const playClip = inject('playClip')
const summarizing = ref(false)

async function summarize() {
  summarizing.value = true
  try {
    emit('updated', await api.refreshSummary(token.value, props.detail.place.id))
  } catch (err) {
    report(err)
  } finally {
    summarizing.value = false
  }
}
</script>

<template>
  <div class="detail">
    <div class="row spread">
      <h2>{{ detail.place.name }}</h2>
      <button class="small secondary" title="Close" @click="emit('close')">✕</button>
    </div>
    <p class="muted">
      {{ detail.place.category }}<template v-if="detail.place.area"> · {{ detail.place.area }}</template>
      <template v-if="detail.place.address"> · {{ detail.place.address }}</template>
      <span v-if="detail.place.aliases.length"><br />Also called: {{ detail.place.aliases.join(', ') }}</span>
    </p>
    <div class="row wrap">
      <button class="small" @click="emit('ask', detail.place)">Ask about this place</button>
      <button class="small secondary" @click="emit('pin', detail.place)">
        {{ detail.place.lat == null ? 'Pin on map' : 'Move pin' }}
      </button>
    </div>
    <p v-if="detail.place.lat == null" class="hint">Not found on OpenStreetMap yet: pin it on the map to use it in trips.</p>
    <p v-else-if="!detail.osm" class="hint">Not on OpenStreetMap: the pin comes from the address in the video (street level) or from you. Move it if it is off.</p>

    <dl v-if="detail.card">
      <template v-if="detail.place.price"><dt>Price</dt><dd>{{ detail.place.price }}</dd></template>
      <template v-if="detail.card.opening_hours">
        <dt>Hours</dt>
        <dd>{{ detail.card.opening_hours.value }} <span class="muted">({{ detail.card.opening_hours.source }})</span></dd>
      </template>
      <template v-if="detail.card.best_time?.length"><dt>Best time</dt><dd>{{ detail.card.best_time.map((b) => b.text).join('; ') }}</dd></template>
      <template v-if="detail.card.pros?.length"><dt>Strengths</dt><dd>{{ detail.card.pros.map((b) => b.text).join('; ') }}</dd></template>
      <template v-if="detail.card.cons?.length"><dt>Weaknesses</dt><dd>{{ detail.card.cons.map((b) => b.text).join('; ') }}</dd></template>
      <template v-if="detail.card.recommendation"><dt>Recommended</dt><dd>{{ detail.card.recommendation.mean }}/5 ({{ detail.card.recommendation.count }} videos)</dd></template>
    </dl>

    <p v-if="detail.summary"><em>{{ detail.summary }}</em></p>
    <p v-else class="muted small-text">
      No summary yet.
      <button class="small secondary" :disabled="summarizing" @click="summarize">{{ summarizing ? 'Summarizing…' : 'Summarize' }}</button>
    </p>

    <div v-for="c in detail.conflicts" :key="c.field" class="conflict">
      <strong>Videos disagree on {{ c.field.replace('_', ' ') }}:</strong>
      <ul>
        <li v-for="(v, i) in c.values" :key="i">{{ v.value }} <span class="muted">— {{ v.video_name || 'OpenStreetMap' }} {{ v.date || '' }}</span></li>
      </ul>
    </div>

    <h3>From your videos</h3>
    <ul class="mentions">
      <li v-for="m in detail.mentions" :key="m.id">
        <button class="small secondary" @click="playClip(m.video_id, m.start_ms, detail.place.name)">▶ {{ timestamp(m.start_ms) }}</button>
        {{ m.video_name }} <span class="muted">({{ m.video_date }}) as “{{ m.name }}”</span>
      </li>
    </ul>
  </div>
</template>
