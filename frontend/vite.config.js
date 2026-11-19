import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The dev server proxies /api to the backend, so the browser sees a single origin.
const apiTarget = process.env.API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
    },
  },
})
