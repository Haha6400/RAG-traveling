<script setup>
// Phase 2: Q&A over the user's own videos, places and trips.
import { inject, ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({ scope: { type: Object, default: null } }) // {id, name} of a place
const emit = defineEmits(['clear-scope'])
const token = inject('token')
const playClip = inject('playClip')
const report = inject('report')

const question = ref('')
const busy = ref(false)
const history = ref([])
const EXAMPLES = [
  'Người review có nói gì về chỗ ngồi ở quán phở Lò Đúc không?',
  'Quán nào mở muộn sau 11 giờ đêm?',
  'Cà phê Giảng mở cửa mấy giờ?',
  'Từ Văn Miếu tới phở Thìn bao xa?',
  'Where can I watch the sunset at a temple?',
  'Ngày 2 trong lịch trình đi đâu?',
]

watch(
  () => props.scope,
  () => (question.value = ''),
)

async function ask(text = question.value) {
  if (!text.trim()) return
  busy.value = true
  try {
    const answer = await api.ask(token.value, { question: text, place_id: props.scope?.id ?? null })
    history.value.unshift({ question: text, scope: props.scope?.name, ...answer })
    question.value = ''
  } catch (err) {
    report(err)
  } finally {
    busy.value = false
  }
}

const fmt = (ms) => `${String(Math.floor(ms / 60000)).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`
</script>

<template>
  <section class="card">
    <h2>Ask your videos</h2>
    <p v-if="scope" class="scope">
      Asking about <strong>{{ scope.name }}</strong>
      <button class="small secondary" @click="emit('clear-scope')">✕ whole map</button>
    </p>
    <form class="row" @submit.prevent="ask()">
      <input v-model="question" class="grow" placeholder="e.g. Quán nào mở muộn?" :disabled="busy" />
      <button type="submit" :disabled="busy || !question.trim()">{{ busy ? 'Thinking…' : 'Ask' }}</button>
    </form>
    <p class="examples">
      <button v-for="e in EXAMPLES" :key="e" class="chip" type="button" @click="ask(e)">{{ e }}</button>
    </p>
  </section>

  <section v-for="(h, i) in history" :key="i" class="card answer">
    <p class="muted">
      {{ h.question }} <span v-if="h.scope">· about {{ h.scope }}</span>
      <span class="badge">{{ h.intent }}</span>
      <span v-if="h.no_data" class="badge failed">no data</span>
      <span v-if="!h.llm_used" class="badge needs_review">no LLM</span>
    </p>
    <p class="answer-text">{{ h.answer }}</p>
    <ul v-if="h.sources.length" class="sources">
      <li v-for="s in h.sources" :key="s.label">
        <strong>[{{ s.label }}]</strong>
        <template v-if="s.kind === 'chunk'">
          <button class="small secondary" @click="playClip(s.video_id, s.start_ms, s.video_name)">▶ {{ fmt(s.start_ms) }}</button>
          {{ s.video_name }}<span v-if="s.place_name" class="muted"> · {{ s.place_name }}</span>
        </template>
        <template v-else>Place card: {{ s.place_name }}</template>
        <div class="muted snippet">{{ s.snippet }}</div>
      </li>
    </ul>
    <p v-if="h.places.length" class="muted">
      On your map:
      <RouterLink v-for="p in h.places" :key="p.id" :to="{ name: 'map', query: { place: p.id } }" class="chip static">📍 {{ p.name }}</RouterLink>
    </p>
  </section>
</template>
