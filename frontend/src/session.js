// Logged-in user and token, shared by the router guard, the shell and every view.
import { computed, reactive } from 'vue'
import { api, loadToken, saveToken } from './api.js'
import { closeAllJobStreams } from './composables/useJobs.js'

export const session = reactive({
  token: loadToken(),
  user: null,
  error: '',
})

export const token = computed(() => session.token)

export function report(err) {
  session.error = err?.message ?? String(err)
}

export function clearError() {
  session.error = ''
}

/** Load the current user for a stored token; drops the token if the API rejects it. */
export async function ensureUser() {
  if (!session.token) return null
  if (session.user) return session.user
  try {
    session.user = await api.me(session.token)
  } catch (err) {
    if (err.status === 401) logout()
    else report(err)
  }
  return session.user
}

export async function login(email, password) {
  const { access_token } = await api.login(email, password)
  session.token = access_token
  saveToken(access_token)
  session.user = null
  return ensureUser()
}

export async function register(email, password) {
  await api.register(email, password)
  return login(email, password)
}

export function logout() {
  closeAllJobStreams()
  session.token = null
  session.user = null
  saveToken(null)
}
