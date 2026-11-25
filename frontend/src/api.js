// Thin wrapper around the backend API. All paths are relative; Vite proxies /api.

const TOKEN_KEY = 'travelrag.token'

export function loadToken() {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function saveToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* storage unavailable: the session just won't survive a reload */
  }
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail))
    this.status = status
  }
}

async function request(method, path, { token, body } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = response.status === 204 ? null : await response.json().catch(() => null)
  if (!response.ok) {
    const detail = data?.detail
    const message = Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : detail
    throw new ApiError(response.status, message ?? response.statusText)
  }
  return data
}

export const api = {
  health: () => request('GET', '/health'),
  register: (email, password) => request('POST', '/auth/register', { body: { email, password } }),
  login: (email, password) => request('POST', '/auth/login', { body: { email, password } }),
  me: (token) => request('GET', '/auth/me', { token }),
  listJobs: (token) => request('GET', '/jobs', { token }),
  createDummyJob: (token, options) => request('POST', '/jobs/dummy', { token, body: options }),
  retryJob: (token, id) => request('POST', `/jobs/${id}/retry`, { token }),
  approveJob: (token, id) => request('POST', `/jobs/${id}/review/approve`, { token }),
  jobEvents: (token, id) =>
    new EventSource(`/api/jobs/${id}/events?token=${encodeURIComponent(token)}`),
  listVideos: (token) => request('GET', '/videos', { token }),
  deleteVideo: (token, id) => request('DELETE', `/videos/${id}`, { token }),
  videoFileUrl: (token, id) => `/api/videos/${id}/file?token=${encodeURIComponent(token)}`,
  uploadVideo,
  transcript: (token, videoId) => request('GET', `/videos/${videoId}/transcript`, { token }),
  videoExtraction: (token, videoId) => request('GET', `/videos/${videoId}/extraction`, { token }),

  // Phase 1: places and review
  listPlaces: (token) => request('GET', '/places', { token }),
  getPlace: (token, id) => request('GET', `/places/${id}`, { token }),
  refreshSummary: (token, id) => request('POST', `/places/${id}/summary`, { token }),
  setLocation: (token, id, lat, lng) =>
    request('PATCH', `/places/${id}/location`, { token, body: { lat, lng } }),
  listReviews: (token) => request('GET', '/reviews', { token }),
  acceptCandidate: (token, id) => request('POST', `/reviews/candidates/${id}/accept`, { token }),
  rejectMention: (token, id) => request('POST', `/reviews/mentions/${id}/reject`, { token }),

  // Phase 2: Q&A
  ask: (token, body) => request('POST', '/ask', { token, body }),

  // Phase 3: trips
  listTrips: (token) => request('GET', '/trips', { token }),
  createTrip: (token, body) => request('POST', '/trips', { token, body }),
  deleteTrip: (token, id) => request('DELETE', `/trips/${id}`, { token }),
  replaceItem: (token, tripId, itemId, placeId) =>
    request('POST', `/trips/${tripId}/items/${itemId}/replace`, {
      token,
      body: placeId ? { place_id: placeId } : {},
    }),
  removeItem: (token, tripId, itemId) => request('DELETE', `/trips/${tripId}/items/${itemId}`, { token }),
  lockItem: (token, tripId, itemId, locked) =>
    request('PATCH', `/trips/${tripId}/items/${itemId}`, { token, body: { locked } }),
}

// XMLHttpRequest instead of fetch: fetch cannot report upload progress.
function uploadVideo(token, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/videos')
    xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100))
    xhr.onload = () => {
      let data = null
      try {
        data = JSON.parse(xhr.responseText)
      } catch {
        /* non-JSON error page */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data)
      else {
        const detail = data?.detail
        reject(new ApiError(xhr.status, detail?.message ?? detail ?? xhr.statusText))
      }
    }
    xhr.onerror = () => reject(new ApiError(0, 'Network error during upload'))
    const form = new FormData()
    form.append('file', file)
    xhr.send(form)
  })
}
