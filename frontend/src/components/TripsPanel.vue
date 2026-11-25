<script setup>
// Phase 3: build an itinerary from your places, then edit it stop by stop.
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { api } from '../api.js'

const token = inject('token')
const report = inject('report')

const trips = ref([])
const current = ref(null)
const places = ref([])
const busy = ref(false)
const alternatives = ref({}) // item id -> alternatives from the last replace
const today = new Date().toISOString().slice(0, 10)
const form = reactive({
  title: '',
  area: 'Hà Nội',
  start_date: today,
  end_date: today,
  budget: '',
  pace: 'balanced',
  priorities: ['food', 'culture'],
  transport: 'motorbike',
  day_start: '08:00',
  day_end: '21:30',
  hotel_place_id: '',
})
const PRIORITIES = ['food', 'photo', 'culture', 'shopping', 'nightlife']
const hotels = computed(() => places.value.filter((p) => p.category === 'hotel' && p.lat != null))
const days = computed(() => {
  const out = {}
  for (const item of current.value?.items ?? []) (out[item.day] ??= []).push(item)
  return Object.entries(out).map(([day, items]) => ({ day: Number(day), date: items[0].date, items }))
})

async function refresh() {
  try {
    ;[trips.value, places.value] = await Promise.all([api.listTrips(token.value), api.listPlaces(token.value)])
    if (current.value) current.value = trips.value.find((t) => t.id === current.value.id) ?? null
  } catch (err) {
    report(err)
  }
}

async function create() {
  busy.value = true
  try {
    current.value = await api.createTrip(token.value, {
      title: form.title || null,
      area: form.area || null,
      start_date: form.start_date,
      end_date: form.end_date,
      preferences: {
        budget: form.budget === '' ? null : Number(form.budget),
        pace: form.pace,
        priorities: form.priorities,
        transport: form.transport,
        day_start: form.day_start,
        day_end: form.day_end,
        hotel_place_id: form.hotel_place_id || null,
      },
    })
    alternatives.value = {}
    await refresh()
  } catch (err) {
    report(err)
  } finally {
    busy.value = false
  }
}

async function replace(item, placeId = null) {
  try {
    const result = await api.replaceItem(token.value, current.value.id, item.id, placeId)
    current.value = result.trip
    alternatives.value = { [item.id]: result.alternatives }
  } catch (err) {
    report(err)
  }
}

async function remove(item) {
  try {
    current.value = await api.removeItem(token.value, current.value.id, item.id)
  } catch (err) {
    report(err)
  }
}

async function toggleLock(item) {
  try {
    current.value = await api.lockItem(token.value, current.value.id, item.id, !item.locked)
  } catch (err) {
    report(err)
  }
}

async function removeTrip(trip) {
  if (!confirm(`Delete "${trip.title}"?`)) return
  try {
    await api.deleteTrip(token.value, trip.id)
    if (current.value?.id === trip.id) current.value = null
    await refresh()
  } catch (err) {
    report(err)
  }
}

const km = (m) => (m / 1000).toFixed(1)
const money = (v) => (v == null ? '' : v.toLocaleString('vi-VN'))
onMounted(refresh)
</script>

<template>
  <section class="card">
    <h2>New trip</h2>
    <form class="grid" @submit.prevent="create">
      <label>Title <input v-model="form.title" placeholder="optional" /></label>
      <label>Area <input v-model="form.area" placeholder="e.g. Hà Nội, Hoàn Kiếm" /></label>
      <label>From <input v-model="form.start_date" type="date" required /></label>
      <label>To <input v-model="form.end_date" type="date" required /></label>
      <label>Budget (VND, total) <input v-model="form.budget" type="number" min="0" step="10000" placeholder="no limit" /></label>
      <label>Pace
        <select v-model="form.pace"><option value="relaxed">relaxed</option><option value="balanced">balanced</option><option value="tight">tight</option></select>
      </label>
      <label>Getting around
        <select v-model="form.transport"><option value="walk">walk</option><option value="motorbike">motorbike</option><option value="car">car</option></select>
      </label>
      <label>Day <span class="row"><input v-model="form.day_start" type="time" /> – <input v-model="form.day_end" type="time" /></span></label>
      <label>Hotel
        <select v-model="form.hotel_place_id"><option value="">none</option><option v-for="h in hotels" :key="h.id" :value="h.id">{{ h.name }}</option></select>
      </label>
      <fieldset>
        <legend>More of</legend>
        <label v-for="p in PRIORITIES" :key="p" class="inline"><input v-model="form.priorities" type="checkbox" :value="p" /> {{ p }}</label>
      </fieldset>
      <button type="submit" :disabled="busy">{{ busy ? 'Planning…' : 'Build itinerary' }}</button>
    </form>
  </section>

  <section v-if="trips.length" class="card">
    <h2>Your trips</h2>
    <ul class="trips">
      <li v-for="t in trips" :key="t.id">
        <a href="#" @click.prevent="current = t; alternatives = {}">{{ t.title }}</a>
        <span class="muted"> {{ t.start_date }} → {{ t.end_date }} · {{ t.items.length }} stops</span>
        <button class="small danger" @click="removeTrip(t)">Delete</button>
      </li>
    </ul>
  </section>

  <section v-if="current" class="card">
    <div class="row spread">
      <h2>{{ current.title }}</h2>
      <span class="muted">
        travel times: {{ current.routing_source === 'osrm' ? 'OSRM' : 'straight-line estimate' }}
        <template v-if="current.total_cost_estimate"> · est. {{ money(current.total_cost_estimate) }} {{ current.preferences.currency }}</template>
      </span>
    </div>
    <p v-if="!current.items.length" class="muted">Nothing could be scheduled — add places (with locations) first.</p>
    <div v-for="d in days" :key="d.day" class="day">
      <h3>Day {{ d.day + 1 }} <span class="muted">{{ d.date }}</span></h3>
      <ol class="itinerary">
        <li v-for="item in d.items" :key="item.id" :class="{ locked: item.locked }">
          <div v-if="item.travel_from_prev" class="leg muted">↓ {{ km(item.travel_from_prev.distance_m) }} km · {{ item.travel_from_prev.duration_min }} min</div>
          <div class="row spread">
            <div>
              <strong>{{ item.start }}–{{ item.end }}</strong> {{ item.place_name }}
              <span class="muted">{{ item.category }}<template v-if="item.price"> · {{ item.price }}</template></span>
            </div>
            <div class="actions">
              <button class="small secondary" :title="item.locked ? 'Unlock' : 'Lock'" @click="toggleLock(item)">{{ item.locked ? '🔒' : '🔓' }}</button>
              <button class="small" :disabled="item.locked" @click="replace(item)">Replace</button>
              <button class="small danger" :disabled="item.locked" @click="remove(item)">Remove</button>
            </div>
          </div>
          <div v-if="item.note" class="note">{{ item.note }}</div>
          <div v-if="alternatives[item.id]?.length" class="alts muted">
            Other options for this slot:
            <button v-for="a in alternatives[item.id]" :key="a.place_id" class="chip" @click="replace(item, a.place_id)">
              {{ a.name }} ({{ a.start }}, {{ km(a.distance_m) }} km)
            </button>
          </div>
        </li>
      </ol>
    </div>
    <details v-if="current.unscheduled.length">
      <summary class="muted">{{ current.unscheduled.length }} places not scheduled</summary>
      <ul>
        <li v-for="u in current.unscheduled" :key="u.place_id">{{ u.name }} <span class="muted">— {{ u.reason.replaceAll('_', ' ') }}</span></li>
      </ul>
    </details>
  </section>
</template>
