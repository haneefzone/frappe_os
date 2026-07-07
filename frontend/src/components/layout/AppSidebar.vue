<template>
  <aside
    class="flex h-full flex-col border-r transition-all duration-150 ease-out"
    :class="collapsed ? 'w-14' : 'w-56'"
    :style="{ background: 'var(--bg-surface)', borderColor: 'var(--border)' }"
  >
    <div class="flex items-center gap-2 px-4 py-4" :class="collapsed && 'justify-center px-0'">
      <div
        class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border text-xs font-bold"
        :style="{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }"
      >
        F
      </div>
      <span
        v-if="!collapsed"
        class="truncate text-sm font-semibold"
        :style="{ color: 'var(--text-primary)' }"
      >
        FDM Platform
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
    </div>
  </aside>
</template>

<script setup lang="ts">
import { navGroups, settingsItem } from '../../navigation'
import SidebarLink from './SidebarLink.vue'

defineProps<{ collapsed: boolean }>()

const version = __APP_VERSION__
</script>
