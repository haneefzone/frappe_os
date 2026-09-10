import type { Component } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { allNavItems } from '../navigation'
import AppsPage from '../pages/AppsPage.vue'
import AuditPage from '../pages/AuditPage.vue'
import BackupsPage from '../pages/BackupsPage.vue'
import BenchesPage from '../pages/BenchesPage.vue'
import DashboardPage from '../pages/DashboardPage.vue'
import JobsPage from '../pages/JobsPage.vue'
import PlaceholderPage from '../pages/PlaceholderPage.vue'
import RestorePage from '../pages/RestorePage.vue'
import MonitoringPage from '../pages/MonitoringPage.vue'
import ReportsPage from '../pages/ReportsPage.vue'
import SchedulesPage from '../pages/SchedulesPage.vue'
import ServersPage from '../pages/ServersPage.vue'
import SettingsPage from '../pages/SettingsPage.vue'
import SitesPage from '../pages/SitesPage.vue'
import TerminalPage from '../pages/TerminalPage.vue'

// Real screens replace the placeholder as each session lands one.
const pageOverrides: Record<string, Component> = {
  dashboard: DashboardPage,
  servers: ServersPage,
  benches: BenchesPage,
  sites: SitesPage,
  apps: AppsPage,
  backups: BackupsPage,
  restore: RestorePage,
  schedules: SchedulesPage,
  jobs: JobsPage,
  monitoring: MonitoringPage,
  reports: ReportsPage,
  terminal: TerminalPage,
  'audit-log': AuditPage,
  settings: SettingsPage,
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
      // Literal route before the :id param route so "new" isn't read as an id.
      name: 'bench-create',
      path: '/benches/new',
      component: () => import('../pages/CreateBenchPage.vue'),
      meta: { label: 'Create bench' },
    },
    {
      name: 'bench-detail',
      path: '/benches/:id',
      component: () => import('../pages/BenchDetailPage.vue'),
      meta: { label: 'Bench' },
    },
    {
      // Literal route before the :id param route so "new" isn't read as an id.
      name: 'site-create',
      path: '/sites/new',
      component: () => import('../pages/CreateSitePage.vue'),
      meta: { label: 'Create site' },
    },
    {
      name: 'site-detail',
      path: '/sites/:id',
      component: () => import('../pages/SiteDetailPage.vue'),
      meta: { label: 'Site' },
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
