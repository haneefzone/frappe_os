import { createRouter, createWebHistory } from 'vue-router'
import { allNavItems } from '../navigation'
import PlaceholderPage from '../pages/PlaceholderPage.vue'

// One route per sidebar item; all render the placeholder until real screens land.
const routes = allNavItems.map((item) => ({
  name: item.name,
  path: item.path,
  component: PlaceholderPage,
  meta: { label: item.label, description: item.description },
}))

export const router = createRouter({
  history: createWebHistory(),
  routes: [...routes, { path: '/:pathMatch(.*)*', redirect: '/' }],
})
