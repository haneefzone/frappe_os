import type { Component } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { allNavItems } from '../navigation'
import JobsPage from '../pages/JobsPage.vue'
import PlaceholderPage from '../pages/PlaceholderPage.vue'
import ServersPage from '../pages/ServersPage.vue'
import TerminalPage from '../pages/TerminalPage.vue'

// Real screens replace the placeholder as each session lands one.
const pageOverrides: Record<string, Component> = {
  servers: ServersPage,
  jobs: JobsPage,
  terminal: TerminalPage,
}

// One route per sidebar item; unbuilt ones render the placeholder.
const routes = allNavItems.map((item) => ({
  name: item.name,
  path: item.path,
  component: pageOverrides[item.name] ?? PlaceholderPage,
  meta: { label: item.label, description: item.description },
}))

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      name: 'login',
      path: '/login',
      component: () => import('../pages/LoginPage.vue'),
      // public routes render without the app chrome and skip the auth guard.
      meta: { label: 'Sign in', public: true },
    },
    ...routes,
    {
      name: 'server-detail',
      path: '/servers/:id',
      component: () => import('../pages/ServerDetailPage.vue'),
      meta: { label: 'Server' },
    },
    {
      name: 'job-detail',
      path: '/jobs/:id',
      component: () => import('../pages/JobDetailPage.vue'),
      meta: { label: 'Job' },
    },
    {
      // Living demo of the component library — not in the sidebar on purpose.
      name: 'styleguide',
      path: '/styleguide',
      component: () => import('../pages/StyleguidePage.vue'),
      meta: { label: 'Styleguide' },
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  // Imported lazily: the store needs pinia, which main.ts installs after
  // this module is evaluated.
  const { useAuthStore } = await import('../stores/auth')
  const auth = useAuthStore()

  if (!auth.initialized) await auth.bootstrap()

  if (to.meta.public) {
    // A signed-in user has no business on the login page.
    return auth.isAuthenticated && to.name === 'login' ? { path: '/' } : true
  }
  if (!auth.isAuthenticated) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }
  return true
})
