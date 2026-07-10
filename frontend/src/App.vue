<template>
  <!-- Public pages (login) render bare, without the app chrome. -->
  <RouterView v-if="route.meta.public" />
  <div v-else class="flex h-screen" :style="{ background: 'var(--bg-base)' }">
    <AppSidebar :collapsed="sidebarCollapsed" />
    <div class="flex min-w-0 flex-1 flex-col">
      <AppTopbar @toggle-sidebar="toggleSidebar" @open-palette="paletteRef?.openPalette()" />
      <main class="min-h-0 flex-1 overflow-y-auto">
        <RouterView />
      </main>
    </div>
    <!-- Global job tray: bottom-right, present on every authenticated screen. -->
    <JobTray />
    <!-- ⌘K command palette: global, portal-rendered over all content. -->
    <CommandPalette ref="paletteRef" />
  </div>
  <ToastHost />
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './components/layout/AppSidebar.vue'
import AppTopbar from './components/layout/AppTopbar.vue'
import CommandPalette from './components/CommandPalette.vue'
import JobTray from './components/JobTray.vue'
import ToastHost from './components/ToastHost.vue'
import { useKeyboardShortcuts } from './composables/useKeyboardShortcuts'
import { useAuthStore } from './stores/auth'
import { useNotificationsStore } from './stores/notifications'
import { useSettingsStore } from './stores/settings'

const COLLAPSE_KEY = 'fdm-sidebar-collapsed'

const route = useRoute()
const sidebarCollapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1')
const paletteRef = ref<InstanceType<typeof CommandPalette> | null>(null)

// Load white-label branding once the operator is authenticated so the sidebar
// shows the configured product name + logo (falls back to defaults otherwise).
const auth = useAuthStore()
const settingsStore = useSettingsStore()
const notificationsStore = useNotificationsStore()

watch(
  () => auth.isAuthenticated,
  (authed) => {
    if (authed) {
      void settingsStore.load()
      notificationsStore.startPolling()
    } else {
      notificationsStore.stopPolling()
    }
  },
  { immediate: true },
)

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem(COLLAPSE_KEY, sidebarCollapsed.value ? '1' : '0')
}

// Global keyboard shortcuts (⌘K, g d, g j, t, /).
useKeyboardShortcuts({ palette: paletteRef })
</script>
