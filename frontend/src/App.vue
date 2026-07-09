<template>
  <!-- Public pages (login) render bare, without the app chrome. -->
  <RouterView v-if="route.meta.public" />
  <div v-else class="flex h-screen" :style="{ background: 'var(--bg-base)' }">
    <AppSidebar :collapsed="sidebarCollapsed" />
    <div class="flex min-w-0 flex-1 flex-col">
      <AppTopbar @toggle-sidebar="toggleSidebar" />
      <main class="min-h-0 flex-1 overflow-y-auto">
        <RouterView />
      </main>
    </div>
    <!-- Global job tray: bottom-right, present on every authenticated screen. -->
    <JobTray />
  </div>
  <ToastHost />
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './components/layout/AppSidebar.vue'
import AppTopbar from './components/layout/AppTopbar.vue'
import JobTray from './components/JobTray.vue'
import ToastHost from './components/ToastHost.vue'

const COLLAPSE_KEY = 'fdm-sidebar-collapsed'

const route = useRoute()
const sidebarCollapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1')

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem(COLLAPSE_KEY, sidebarCollapsed.value ? '1' : '0')
}
</script>
