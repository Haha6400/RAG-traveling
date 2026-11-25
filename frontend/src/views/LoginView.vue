<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { login, register } from '../session.js'

const route = useRoute()
const router = useRouter()
const form = reactive({ email: '', password: '' })
const error = ref('')
const busy = ref(false)

async function submit(action) {
  error.value = ''
  busy.value = true
  try {
    await (action === 'register' ? register : login)(form.email, form.password)
    router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : '/')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login">
    <section class="card">
      <h1>RAG-traveling</h1>
      <p class="muted">Your travel map, built from the review videos you watch.</p>
      <form @submit.prevent="submit('login')">
        <input v-model="form.email" type="email" placeholder="email" autocomplete="username" required />
        <input v-model="form.password" type="password" placeholder="password (8+ characters)" minlength="8" autocomplete="current-password" required />
        <div class="row">
          <button type="submit" :disabled="busy">Log in</button>
          <button type="button" class="secondary" :disabled="busy" @click="submit('register')">Register</button>
        </div>
      </form>
      <p v-if="error" class="error">{{ error }}</p>
    </section>
  </div>
</template>
