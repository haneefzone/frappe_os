<template>
  <aside
    class="flex h-full flex-col border-r transition-all duration-150 ease-out"
    :class="collapsed ? 'w-14' : 'w-56'"
    :style="{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }"
  >
    <div class="flex items-center gap-2 px-4 py-4" :class="collapsed && 'justify-center px-0'">
      <div
        class="flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden rounded-md border text-xs font-bold"
        :style="{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }"
      >
        <img
          v-if="effectiveLogoUrl"
          :src="effectiveLogoUrl"
          :alt="productName"
          class="h-full w-full object-contain"
          @error="logoError = true"
        />
        <template v-else>{{ productName.charAt(0).toUpperCase() }}</template>
      </div>
      <span
        v-if="!collapsed"
        class="truncate text-sm font-semibold"
        :style="{ color: 'var(--text-primary)' }"
      >
        {{ productName }}
      </span>
    </div>

    <nav class="flex-1 overflow-y-auto px-2 pb-2">
      <template v-for="group in navGroups" :key="group.label ?? 'top'">
        <div
          v-if="group.label && !collapsed"
          class="mt-4 mb-1 px-2 text-[10px] font-semibold uppercase tracking-widest"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ group.label }}
        </div>
        <div v-else-if="group.label" class="mt-3 mb-1 border-t" :style="{ borderColor: 'var(--border)' }" />
        <SidebarLink
          v-for="item in group.items"
          :key="item.name"
          :item="item"
          :collapsed="collapsed"
        />
      </template>
    </nav>

    <div class="border-t px-2 py-2" :style="{ borderColor: 'var(--border)' }">
      <SidebarLink :item="settingsItem" :collapsed="collapsed" />
      <div
        v-if="!collapsed"
        class="px-2 pt-1 pb-1 text-[10px]"
        :style="{ color: 'var(--text-muted)' }"
      >
        v{{ version }}
      </div>
      <!-- Optional footer line from brand settings -->
      <div
        v-if="!collapsed && footerLine"
        class="truncate px-2 pb-0.5 text-[10px]"
        :style="{ color: 'var(--text-muted)' }"
        :title="footerLine"
      >
        {{ footerLine }}
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { navGroups, settingsItem } from '../../navigation'
import { useSettingsStore } from '../../stores/settings'
import { useTheme } from '../../composables/useTheme'
import SidebarLink from './SidebarLink.vue'

defineProps<{ collapsed: boolean }>()

const settingsStore = useSettingsStore()
const { productName, logoUrl, logoDarkUrl, footerLine } = storeToRefs(settingsStore)
const { theme } = useTheme()

// Track per-session load errors so a broken image falls back to the wordmark.
const logoError = ref(false)

// Use dark logo in dark theme (falls back to light logo, then wordmark).
// Reset error flag whenever the URL changes so a fresh upload recovers.
const effectiveLogoUrl = computed(() => {
  if (logoError.value) return null
  if (theme.value === 'dark' && logoDarkUrl.value) return logoDarkUrl.value
  return logoUrl.value || null
})

const version = __APP_VERSION__
</script>
