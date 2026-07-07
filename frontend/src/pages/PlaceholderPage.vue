<template>
  <div class="flex h-full flex-col">
    <header class="border-b px-8 py-5" :style="{ borderColor: 'var(--border)' }">
      <h1 class="text-lg font-semibold" :style="{ color: 'var(--text-primary)' }">
        {{ item?.label ?? String(route.meta.label ?? '') }}
      </h1>
    </header>
    <div class="flex flex-1 items-center justify-center">
      <EmptyState
        :icon="item?.icon"
        :title="`${item?.label ?? 'This screen'} is coming soon`"
        :message="String(route.meta.description ?? 'This part of the platform has not been built yet.')"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import EmptyState from '../components/EmptyState.vue'
import { allNavItems } from '../navigation'

const route = useRoute()
const item = computed(() => allNavItems.find((entry) => entry.name === route.name))
</script>
