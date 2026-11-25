import { createRouter, createWebHistory } from 'vue-router'
import { ensureUser, session } from './session.js'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/LoginView.vue'), meta: { public: true } },
  { path: '/', name: 'map', component: () => import('./views/MapView.vue'), meta: { title: 'Map' } },
  { path: '/videos', name: 'videos', component: () => import('./views/VideosView.vue'), meta: { title: 'Videos' } },
  { path: '/ask', name: 'ask', component: () => import('./views/AskView.vue'), meta: { title: 'Ask' } },
  { path: '/trips', name: 'trips', component: () => import('./views/TripsView.vue'), meta: { title: 'Trips' } },
  { path: '/dev', name: 'dev', component: () => import('./views/DevView.vue'), meta: { title: 'Dev tools' } },
  { path: '/:rest(.*)*', redirect: '/' },
]

export const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  if (!session.token || !(await ensureUser())) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · RAG-traveling` : 'RAG-traveling'
})
