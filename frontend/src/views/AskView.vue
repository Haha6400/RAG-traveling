<script setup>
// Q&A; ?place=<id>&name=<name> scopes the questions to one place ("Ask about this place").
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AskPanel from '../components/AskPanel.vue'

const route = useRoute()
const router = useRouter()
const scope = computed(() =>
  typeof route.query.place === 'string'
    ? { id: route.query.place, name: typeof route.query.name === 'string' ? route.query.name : 'this place' }
    : null,
)
</script>

<template>
  <AskPanel :scope="scope" @clear-scope="router.replace({ name: 'ask' })" />
</template>
