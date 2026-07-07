<template>
  <RouterLink
    :to="item.path"
    class="group my-0.5 flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors duration-150 ease-out"
    :class="collapsed && 'justify-center px-0'"
    :style="active
      ? { background: 'var(--bg-raised)', color: 'var(--text-primary)' }
      : { color: 'var(--text-secondary)' }"
    :title="collapsed ? item.label : undefined"
  >
    <component :is="item.icon" class="h-5 w-5 shrink-0" />
    <span v-if="!collapsed" class="truncate">{{ item.label }}</span>
  </RouterLink>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import type { NavItem } from '../../navigation'

const props = defineProps<{ item: NavItem; collapsed: boolean }>()

const route = useRoute()
const active = computed(() => route.name === props.item.name)
</script>
