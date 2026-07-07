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

    <!-- Placeholder search — becomes the ⌘K command palette in a later session. -->
    <div
      class="flex h-8 w-full max-w-md items-center gap-2 rounded-md border px-3 text-sm"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)', background: 'var(--bg-base)' }"
    >
      <LucideSearch class="h-4 w-4 shrink-0" />
      <span class="flex-1 truncate text-left">Search servers, sites, jobs…</span>
      <kbd
        class="rounded border px-1.5 py-0.5 text-[10px]"
        :style="{ borderColor: 'var(--border-strong)' }"
      >
        ⌘K
      </kbd>
    </div>

    <div class="flex-1" />

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
  </header>
</template>

<script setup lang="ts">
import LucideMoon from '~icons/lucide/moon'
import LucidePanelLeft from '~icons/lucide/panel-left'
import LucideSearch from '~icons/lucide/search'
import LucideSun from '~icons/lucide/sun'
import { useTheme } from '../../composables/useTheme'

defineEmits<{ 'toggle-sidebar': [] }>()

const { theme, toggle } = useTheme()
</script>
