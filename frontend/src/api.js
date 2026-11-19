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
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, data?.detail ?? response.statusText)
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
