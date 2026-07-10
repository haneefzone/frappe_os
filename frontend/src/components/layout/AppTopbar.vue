<template>
  <header
    class="flex h-12 shrink-0 items-center gap-3 border-b px-4"
    :style="{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }"
  >
    <button
      type="button"
      class="flex h-8 w-8 items-center justify-center rounded-md transition-colors duration-150 ease-out"
      :style="{ color: 'var(--text-secondary)' }"
      aria-label="Toggle sidebar"
      @click="$emit('toggle-sidebar')"
    >
      <LucidePanelLeft class="h-5 w-5" />
    </button>

    <!-- ⌘K search / command palette trigger -->
    <button
      type="button"
      class="flex h-8 w-full max-w-md items-center gap-2 rounded-md border px-3 text-sm text-left transition-colors duration-150 ease-out hover:border-opacity-80"
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

    <!-- Notification bell -->
    <NotificationDrawer />

    <button
      type="button"
      class="flex h-8 w-8 items-center justify-center rounded-md transition-colors duration-150 ease-out"
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
        class="flex h-7 w-7 items-center justify-center rounded-full border border-line bg-raised text-meta font-semibold text-ink-1"
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
        class="absolute right-0 top-9 z-20 w-56 rounded-lg border border-line bg-raised py-1"
      >
        <div class="border-b border-line px-3 py-2">
          <p class="truncate text-body font-medium text-ink-1">{{ auth.user.full_name }}</p>
          <p class="truncate text-meta text-ink-3">{{ auth.user.email }}</p>
          <span
            class="mt-1.5 inline-block rounded border border-line px-1.5 py-0.5 text-meta text-ink-2"
          >
            {{ auth.user.role }}
          </span>
        </div>
        <button
          type="button"
          role="menuitem"
          class="flex w-full items-center gap-2 px-3 py-2 text-left text-body text-ink-2 transition-colors duration-150 ease-out hover:bg-surface hover:text-ink-1"
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

defineEmits<{ 'toggle-sidebar': []; 'open-palette': [] }>()

const { theme, toggle } = useTheme()
const auth = useAuthStore()

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
