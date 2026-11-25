<script setup>
// The personal map: filterable place list, Leaflet map, and the selected place's card.
// Places without coordinates can be pinned by clicking the map.
import { computed, inject, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import PlaceDetail from '../components/PlaceDetail.vue'
import PlaceMap from '../components/PlaceMap.vue'
import ReviewList from '../components/ReviewList.vue'
import { CATEGORIES, CATEGORY_COLORS, fold } from '../lib/format.js'

const token = inject('token')
const report = inject('report')
const route = useRoute()
const router = useRouter()

const places = ref([])
const reviews = ref([])
const loaded = ref(false)
const selectedId = ref(typeof route.query.place === 'string' ? route.query.place : null)
const detail = ref(null)
const pinTarget = ref(null)
const showReviews = ref(false)
const mapRef = ref(null)

const filters = reactive({ q: '', categories: [], area: '', maxPrice: '', conflictsOnly: false })

const areas = computed(() => [...new Set(places.value.map((p) => p.area).filter(Boolean))].sort())
const counts = computed(() => {
  const out = {}
  for (const p of places.value) out[p.category] = (out[p.category] ?? 0) + 1
  return out
})
const filtered = computed(() => {
  const q = fold(filters.q.trim())
  const max = filters.maxPrice === '' ? null : Number(filters.maxPrice)
  return places.value.filter((p) => {
    if (q && ![p.name, ...p.aliases].some((n) => fold(n).includes(q))) return false
    if (filters.categories.length && !filters.categories.includes(p.category)) return false
    if (filters.area && p.area !== filters.area) return false
    // like the Q&A filters: an unknown price never passes a price limit
    if (max != null && (p.price_min == null || p.price_min > max)) return false
    if (filters.conflictsOnly && !p.has_conflicts) return false
    return true
  })
})
const unlocated = computed(() => filtered.value.filter((p) => p.lat == null))
const activeFilters = computed(
  () => !!(filters.q || filters.categories.length || filters.area || filters.maxPrice !== '' || filters.conflictsOnly),
)

async function load() {
  try {
    ;[places.value, reviews.value] = await Promise.all([api.listPlaces(token.value), api.listReviews(token.value)])
  } catch (err) {
    report(err)
  } finally {
    loaded.value = true
  }
}

async function loadDetail(id) {
  if (!id) {
    detail.value = null
    return
  }
  try {
    detail.value = await api.getPlace(token.value, id)
  } catch (err) {
    report(err)
    selectedId.value = null
  }
}

function select(id) {
  selectedId.value = id
}

function toggleCategory(category) {
  const i = filters.categories.indexOf(category)
  if (i === -1) filters.categories.push(category)
  else filters.categories.splice(i, 1)
}

function resetFilters() {
  Object.assign(filters, { q: '', categories: [], area: '', maxPrice: '', conflictsOnly: false })
}

function startPin(place) {
  pinTarget.value = place
  selectedId.value = place.id
}

async function pick({ lat, lng }) {
  const place = pinTarget.value
  if (!place) return
  if (place.lat != null && !confirm(`Move "${place.name}" here?`)) return
  try {
    await api.setLocation(token.value, place.id, Number(lat.toFixed(6)), Number(lng.toFixed(6)))
    pinTarget.value = null
    await load()
    await loadDetail(place.id)
  } catch (err) {
    report(err)
  }
}

function askAbout(place) {
  router.push({ name: 'ask', query: { place: place.id, name: place.name } })
}

function onKey(event) {
  if (event.key === 'Escape') {
    if (pinTarget.value) pinTarget.value = null
    else selectedId.value = null
  }
}

watch(selectedId, (id) => {
  loadDetail(id)
  router.replace({ query: id ? { place: id } : {} })
})

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  await load()
  if (selectedId.value) loadDetail(selectedId.value)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="map-layout" :class="{ 'with-detail': detail }">
    <aside class="sidebar">
      <div v-if="reviews.length" class="card warn compact">
        <div class="row spread">
          <strong>{{ reviews.length }} place{{ reviews.length > 1 ? 's' : '' }} to confirm</strong>
          <button class="small secondary" @click="showReviews = !showReviews">{{ showReviews ? 'Hide' : 'Review' }}</button>
        </div>
        <ReviewList v-if="showReviews" :reviews="reviews" @changed="load" />
      </div>

      <div class="filters">
        <input v-model="filters.q" type="search" placeholder="Search places (e.g. pho thin)" />
        <div class="chips">
          <button
            v-for="c in CATEGORIES.filter((c) => counts[c])"
            :key="c"
            class="chip"
            :class="{ on: filters.categories.includes(c) }"
            @click="toggleCategory(c)"
          >
            <span class="dot" :style="{ background: CATEGORY_COLORS[c] }"></span>{{ c }} {{ counts[c] }}
          </button>
        </div>
        <div class="row">
          <select v-model="filters.area" class="grow">
            <option value="">All areas</option>
            <option v-for="a in areas" :key="a" :value="a">{{ a }}</option>
          </select>
          <input v-model="filters.maxPrice" type="number" min="0" step="10000" placeholder="max price" class="price" />
        </div>
        <div class="row spread">
          <label class="inline"><input v-model="filters.conflictsOnly" type="checkbox" /> only conflicting info</label>
          <button v-if="activeFilters" class="small secondary" @click="resetFilters">Clear</button>
        </div>
      </div>

      <p class="muted small-text">
        {{ filtered.length }} of {{ places.length }} places
        <template v-if="unlocated.length"> · {{ unlocated.length }} without location</template>
        <button v-if="places.length" class="small secondary" @click="mapRef?.fitAll()">Fit map</button>
      </p>

      <p v-if="loaded && !places.length" class="muted">
        No places yet. Upload a video on the <RouterLink to="/videos">Videos</RouterLink> page, or load the demo data (see README).
      </p>

      <ul class="place-list">
        <li
          v-for="p in filtered"
          :key="p.id"
          :class="{ active: p.id === selectedId }"
          @click="select(p.id)"
        >
          <span class="dot" :style="{ background: CATEGORY_COLORS[p.category] }"></span>
          <div class="grow">
            <strong>{{ p.name }}</strong>
            <div class="muted small-text">
              {{ p.category }}<template v-if="p.rating"> · ★ {{ p.rating }}</template>
              <template v-if="p.price"> · {{ p.price }}</template> · {{ p.videos }} video{{ p.videos === 1 ? '' : 's' }}
            </div>
          </div>
          <span v-if="p.has_conflicts" class="badge needs_review" title="Videos disagree">!</span>
          <button v-if="p.lat == null" class="small" @click.stop="startPin(p)">Pin</button>
        </li>
      </ul>
    </aside>

    <section class="map-area">
      <div v-if="pinTarget" class="pin-banner">
        Click the map to place <strong>{{ pinTarget.name }}</strong>
        <button class="small secondary" @click="pinTarget = null">Cancel (Esc)</button>
      </div>
      <PlaceMap
        ref="mapRef"
        :places="filtered"
        :selected-id="selectedId"
        :pin-target="pinTarget"
        @select="select"
        @pick="pick"
      />
    </section>

    <aside v-if="detail" class="detail-panel">
      <PlaceDetail
        :detail="detail"
        @close="selectedId = null"
        @ask="askAbout"
        @pin="startPin"
        @updated="(d) => (detail = d)"
      />
    </aside>
  </div>
</template>
