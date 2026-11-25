<script setup>
// Leaflet map of the user's places. Raster tiles from tile.openstreetmap.org (development use;
// see docs/frontend-map.md for the usage policy and the planned move to MapLibre GL).
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CATEGORY_COLORS } from '../lib/format.js'

const props = defineProps({
  places: { type: Array, required: true }, // place briefs; those without lat/lng are skipped
  selectedId: { type: String, default: null },
  pinTarget: { type: Object, default: null }, // place being pinned: map clicks pick its location
})
const emit = defineEmits(['select', 'pick'])

const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
const DEFAULT_CENTER = [21.0285, 105.8542] // Hoàn Kiếm, Hà Nội
const DEFAULT_ZOOM = 13

const el = ref(null)
// Leaflet objects stay outside Vue's reactivity on purpose.
let map = null
let layer = null
let resizeObserver = null
const markers = new Map() // place id -> L.CircleMarker
let fitted = false

function markerStyle(place, selected) {
  return {
    radius: selected ? 11 : 7,
    color: selected ? '#111' : '#fff',
    weight: selected ? 3 : place.has_conflicts ? 2.5 : 1.5,
    dashArray: place.has_conflicts && !selected ? '3 2' : null,
    fillColor: CATEGORY_COLORS[place.category] ?? CATEGORY_COLORS.other,
    fillOpacity: 0.95,
  }
}

function draw() {
  layer.clearLayers()
  markers.clear()
  for (const place of props.places) {
    if (place.lat == null || place.lng == null) continue
    const marker = L.circleMarker([place.lat, place.lng], markerStyle(place, place.id === props.selectedId))
      .bindTooltip(place.name, { direction: 'top', offset: [0, -8] })
      .on('click', (event) => {
        L.DomEvent.stopPropagation(event) // a marker click is not a "pick a location" click
        emit('select', place.id)
      })
    marker.addTo(layer)
    markers.set(place.id, marker)
  }
  markers.get(props.selectedId)?.bringToFront()
  if (!fitted && markers.size) {
    fitAll()
    fitted = true
  }
}

function fitAll() {
  const points = [...markers.values()].map((m) => m.getLatLng())
  if (points.length === 1) map.setView(points[0], 16)
  else if (points.length > 1) map.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 16 })
}

function restyle(previousId) {
  for (const id of [previousId, props.selectedId]) {
    const marker = markers.get(id)
    const place = props.places.find((p) => p.id === id)
    if (marker && place) marker.setStyle(markerStyle(place, id === props.selectedId))
  }
  const selected = markers.get(props.selectedId)
  if (selected) {
    selected.bringToFront()
    if (!map.getBounds().pad(-0.1).contains(selected.getLatLng())) {
      map.flyTo(selected.getLatLng(), Math.max(map.getZoom(), 15), { duration: 0.6 })
    }
  }
}

onMounted(() => {
  map = L.map(el.value, { zoomControl: true }).setView(DEFAULT_CENTER, DEFAULT_ZOOM)
  L.tileLayer(TILE_URL, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(map)
  layer = L.layerGroup().addTo(map)
  map.on('click', (event) => {
    if (props.pinTarget) emit('pick', { lat: event.latlng.lat, lng: event.latlng.lng })
  })
  // The map's box changes when the detail panel opens or the window resizes.
  resizeObserver = new ResizeObserver(() => map.invalidateSize())
  resizeObserver.observe(el.value)
  draw()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  map?.remove()
  map = null
})

watch(() => props.places, draw)
watch(() => props.selectedId, (_, previous) => restyle(previous))

defineExpose({ fitAll })
</script>

<template>
  <div ref="el" class="place-map" :class="{ pinning: pinTarget }"></div>
</template>
