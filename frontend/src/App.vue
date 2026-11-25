<script setup>
// App shell: top navigation, error banner, shared video player. Views render in <RouterView>.
import { provide, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import ClipPlayer from './components/ClipPlayer.vue'
import { clearError, logout, report, session, token } from './session.js'

const route = useRoute()
const router = useRouter()
const clip = ref(null) // {videoId, startMs, title}

provide('token', token)
provide('report', report)
provide('playClip', (videoId, startMs, title) => {
  clip.value = { videoId, startMs: startMs ?? 0, title }
})

const NAV = [
  { to: '/', label: 'Map' },
  { to: '/videos', label: 'Videos' },
  { to: '/ask', label: 'Ask' },
  { to: '/trips', label: 'Trips' },
]

function signOut() {
  logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <RouterView v-if="route.meta.public" />
  <div v-else class="shell">
    <header class="topbar">
      <RouterLink to="/" class="brand">RAG-traveling</RouterLink>
      <nav>
        <RouterLink v-for="item in NAV" :key="item.to" :to="item.to" class="navlink" exact-active-class="active">
          {{ item.label }}
        </RouterLink>
      </nav>
      <div class="account">
        <RouterLink to="/dev" class="muted small-text" title="Dummy jobs and event log">dev</RouterLink>
        <span class="muted small-text">{{ session.user?.email }}</span>
        <button class="small secondary" @click="signOut">Log out</button>
      </div>
    </header>
    <p v-if="session.error" class="banner error" @click="clearError">{{ session.error }} <span class="muted">(click to dismiss)</span></p>
    <main :class="route.name === 'map' ? 'full' : 'page'">
      <RouterView />
    </main>
    <ClipPlayer v-if="clip" :clip="clip" :token="token" @close="clip = null" />
  </div>
</template>
