<template>
  <header
    class="flex h-12 shrink-0 items-center gap-3 border-b px-4"
    :style="{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }"
  >
    <button
      type="button"
      class="fdm-focus flex h-8 w-8 items-center justify-center rounded-md transition-colors duration-150 ease-out"
      :style="{ color: 'var(--text-secondary)' }"
      aria-label="Toggle sidebar"
      @click="$emit('toggle-sidebar')"
    >
      <LucidePanelLeft class="h-5 w-5" />
    </button>

    <!-- ⌘K search / command palette trigger -->
    <button
      type="button"
      class="fdm-focus flex h-8 w-full max-w-md items-center gap-2 rounded-md border px-3 text-sm text-left transition-colors duration-150 ease-out hover:border-opacity-80"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)', background: 'var(--bg-base)' }"
      aria-label="Open command palette"
      @click="$emit('open-palette')"
    >
      <LucideSearch class="h-4 w-4 shrink-0" />
      <span class="flex-1 truncate">Search servers, sites, jobs…</span>
      <kbd
        class="rounded border px-1.5 py-0.5 text-[10px]"
        :style="{ borderColor: 'var(--border-strong)' }"
      >
        ⌘K
      </kbd>
    </button>

    <div class="flex-1" />

    <!-- Running jobs indicator: pulsing dot + count, only when jobs are active -->
    <button
      v-if="jobsStore.runningCount > 0"
      type="button"
      class="fdm-focus flex h-8 items-center gap-1.5 rounded-md px-2 transition-colors duration-150 ease-out"
      :style="{ color: 'var(--text-secondary)' }"
      :aria-label="`${jobsStore.runningCount} jobs running — open jobs tray`"
      @click="$emit('open-tray')"
    >
      <span
        class="h-2 w-2 animate-pulse rounded-full"
        :style="{ background: 'var(--status-info)' }"
      />
      <span class="text-xs tabular-nums" :style="{ color: 'var(--text-primary)' }">
        {{ jobsStore.runningCount }}
      </span>
    </button>

    <!-- Notification bell -->
    <NotificationDrawer />

    <button
      type="button"
      class="fdm-focus flex h-8 w-8 items-center justify-center rounded-md transition-colors duration-150 ease-out"
      :style="{ color: 'var(--text-secondary)' }"
      :aria-label="theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'"
      @click="toggle"
    >
      <LucideSun v-if="theme === 'dark'" class="h-5 w-5" />
      <LucideMoon v-else class="h-5 w-5" />
    </button>

    <!-- User menu -->
    <div v-if="auth.user" ref="menuRoot" class="relative">
      <button
        type="button"
        class="fdm-focus flex h-7 w-7 items-center justify-center rounded-full border font-semibold text-sm"
        :style="{ background: 'var(--bg-raised)', borderColor: 'var(--border)', color: 'var(--text-primary)' }"
        aria-label="User menu"
        aria-haspopup="menu"
        :aria-expanded="menuOpen"
        @click="menuOpen = !menuOpen"
      >
        {{ initials }}
      </button>
      <div
        v-if="menuOpen"
        role="menu"
        class="absolute right-0 top-9 z-20 w-56 rounded-lg border py-1"
        :style="{ background: 'var(--bg-raised)', borderColor: 'var(--border-strong)' }"
      >
        <div class="border-b px-3 py-2" :style="{ borderColor: 'var(--border)' }">
          <p class="truncate text-sm font-medium" :style="{ color: 'var(--text-primary)' }">
            {{ auth.user.full_name }}
          </p>
          <p class="truncate text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ auth.user.email }}
          </p>
          <span
            class="mt-1.5 inline-block rounded border px-1.5 py-0.5 text-xs"
            :style="{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }"
          >
            {{ auth.user.role }}
          </span>
        </div>
        <button
          type="button"
          role="menuitem"
          class="fdm-focus flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors duration-150 ease-out hover:bg-white/5"
          :style="{ color: 'var(--text-secondary)' }"
          @click="logout"
        >
          <LucideLogOut class="h-4 w-4" />
          Log out
        </button>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import LucideLogOut from '~icons/lucide/log-out'
import LucideMoon from '~icons/lucide/moon'
import LucidePanelLeft from '~icons/lucide/panel-left'
import LucideSearch from '~icons/lucide/search'
import LucideSun from '~icons/lucide/sun'
import NotificationDrawer from '../NotificationDrawer.vue'
import { useTheme } from '../../composables/useTheme'
import { useAuthStore } from '../../stores/auth'
import { useJobsStore } from '../../stores/jobs'

defineEmits<{ 'toggle-sidebar': []; 'open-palette': []; 'open-tray': [] }>()

const { theme, toggle } = useTheme()
const auth = useAuthStore()
const jobsStore = useJobsStore()

const menuOpen = ref(false)
const menuRoot = ref<HTMLElement | null>(null)

const initials = computed(() => {
  const name = auth.user?.full_name ?? ''
  const parts = name.trim().split(/\s+/).filter(Boolean)
  return parts
    .slice(0, 2)
    .map((part) => part[0]!.toUpperCase())
    .join('')
})

function onDocumentClick(event: MouseEvent) {
  if (menuRoot.value && !menuRoot.value.contains(event.target as Node)) menuOpen.value = false
}

onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))

async function logout() {
  menuOpen.value = false
  await auth.logout()
}
</script>
