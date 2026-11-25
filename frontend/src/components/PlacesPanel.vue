<script setup>
// Phase 1: the personal map as a list of place cards, plus "Is this X?" reviews.
import { inject, onMounted, ref } from 'vue'
import { api } from '../api.js'

const token = inject('token')
const playClip = inject('playClip')
const report = inject('report')
const askAbout = inject('askAbout')

const places = ref([])
const reviews = ref([])
const selected = ref(null)
const loading = ref(false)
const pin = ref({ lat: '', lng: '' })

async function refresh() {
  try {
    ;[places.value, reviews.value] = await Promise.all([
      api.listPlaces(token.value),
      api.listReviews(token.value),
    ])
  } catch (err) {
    report(err)
  }
}

async function open(id) {
  loading.value = true
  try {
    selected.value = await api.getPlace(token.value, id)
    pin.value = { lat: selected.value.place.lat ?? '', lng: selected.value.place.lng ?? '' }
  } catch (err) {
    report(err)
  } finally {
    loading.value = false
  }
}

async function resummarize() {
  try {
    selected.value = await api.refreshSummary(token.value, selected.value.place.id)
  } catch (err) {
    report(err)
  }
}

async function savePin() {
  try {
    await api.setLocation(token.value, selected.value.place.id, Number(pin.value.lat), Number(pin.value.lng))
    await open(selected.value.place.id)
    await refresh()
  } catch (err) {
    report(err)
  }
}

async function accept(candidate) {
  try {
    await api.acceptCandidate(token.value, candidate.id)
    await refresh()
  } catch (err) {
    report(err)
  }
}

async function reject(review) {
  try {
    await api.rejectMention(token.value, review.mention_id)
    await refresh()
  } catch (err) {
    report(err)
  }
}

const fmt = (ms) => `${String(Math.floor(ms / 60000)).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`

defineExpose({ refresh })
onMounted(refresh)
</script>

<template>
  <section v-if="reviews.length" class="card warn">
    <h2>Needs your confirmation <small class="muted">({{ reviews.length }})</small></h2>
    <div v-for="r in reviews" :key="r.mention_id" class="review">
      <p>
        A video mentions <strong>{{ r.name }}</strong>
        <span class="muted">{{ r.area ? `(${r.area})` : '' }} at {{ fmt(r.start_ms) }}</span>.
        Is this one of these?
        <button class="small secondary" @click="playClip(r.video_id, r.start_ms, r.name)">▶ Watch</button>
      </p>
      <ul>
        <li v-for="c in r.candidates" :key="c.id">
          <template v-if="c.place">{{ c.place.name }} <span class="muted">{{ c.place.area || c.place.address }}</span></template>
          <template v-else>{{ c.osm.name }} <span class="muted">(OpenStreetMap) {{ c.osm.address }}</span></template>
          <span class="muted"> · score {{ c.score.toFixed(2) }}</span>
          <button class="small" @click="accept(c)">Yes, it's this</button>
        </li>
      </ul>
      <button class="small secondary" @click="reject(r)">None of these — new place</button>
    </div>
  </section>

  <div class="split">
    <section class="card list">
      <div class="row spread">
        <h2>Places <small class="muted">({{ places.length }})</small></h2>
        <button class="small secondary" @click="refresh">Refresh</button>
      </div>
      <p v-if="!places.length" class="muted">No places yet. Upload a video, or load the demo data (see README).</p>
      <ul class="places">
        <li v-for="p in places" :key="p.id" :class="{ active: selected?.place.id === p.id }" @click="open(p.id)">
          <strong>{{ p.name }}</strong>
          <span class="muted"> {{ p.category }} · {{ p.videos }} video{{ p.videos === 1 ? '' : 's' }}</span>
          <span v-if="p.rating" class="muted"> · ★ {{ p.rating }}</span>
          <span v-if="p.has_conflicts" class="badge needs_review">conflict</span>
          <span v-if="p.lat == null" class="badge failed">no location</span>
        </li>
      </ul>
    </section>

    <section class="card detail">
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!selected" class="muted">Select a place.</p>
      <template v-else>
        <div class="row spread">
          <h2>{{ selected.place.name }}</h2>
          <button class="small" @click="askAbout(selected.place)">Ask about this place</button>
        </div>
        <p class="muted">
          {{ selected.place.category }} · {{ selected.place.area }} · {{ selected.place.address }}
          <span v-if="selected.place.aliases.length"><br />Also called: {{ selected.place.aliases.join(', ') }}</span>
        </p>
        <dl v-if="selected.card">
          <template v-if="selected.place.price"><dt>Price</dt><dd>{{ selected.place.price }}</dd></template>
          <template v-if="selected.card.opening_hours">
            <dt>Hours</dt>
            <dd>{{ selected.card.opening_hours.value }} <span class="muted">({{ selected.card.opening_hours.source }})</span></dd>
          </template>
          <template v-if="selected.card.best_time?.length"><dt>Best time</dt><dd>{{ selected.card.best_time.map((b) => b.text).join('; ') }}</dd></template>
          <template v-if="selected.card.pros?.length"><dt>Strengths</dt><dd>{{ selected.card.pros.map((b) => b.text).join('; ') }}</dd></template>
          <template v-if="selected.card.cons?.length"><dt>Weaknesses</dt><dd>{{ selected.card.cons.map((b) => b.text).join('; ') }}</dd></template>
          <template v-if="selected.card.recommendation"><dt>Recommended</dt><dd>{{ selected.card.recommendation.mean }}/5 ({{ selected.card.recommendation.count }} videos)</dd></template>
        </dl>
        <p v-if="selected.summary"><em>{{ selected.summary }}</em></p>
        <p v-else class="muted">
          No summary yet{{ selected.summary_stale ? ' (needs an LLM key)' : '' }}.
          <button class="small secondary" @click="resummarize">Summarize</button>
        </p>

        <div v-for="c in selected.conflicts" :key="c.field" class="conflict">
          <strong>Videos disagree on {{ c.field }}:</strong>
          <ul>
            <li v-for="(v, i) in c.values" :key="i">{{ v.value }} <span class="muted">— {{ v.video_name || 'OpenStreetMap' }} {{ v.date || '' }}</span></li>
          </ul>
        </div>

        <h3>From your videos</h3>
        <ul class="mentions">
          <li v-for="m in selected.mentions" :key="m.id">
            <button class="small secondary" @click="playClip(m.video_id, m.start_ms, selected.place.name)">▶ {{ fmt(m.start_ms) }}</button>
            {{ m.video_name }} <span class="muted">({{ m.video_date }}) as “{{ m.name }}”</span>
          </li>
        </ul>

        <form v-if="selected.place.lat == null" class="row wrap" @submit.prevent="savePin">
          <span class="muted">Not on OpenStreetMap — pin it:</span>
          <input v-model="pin.lat" placeholder="lat" size="8" required />
          <input v-model="pin.lng" placeholder="lng" size="8" required />
          <button class="small" type="submit">Save location</button>
        </form>
      </template>
    </section>
  </div>
</template>
