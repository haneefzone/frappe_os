<template>
  <!-- Not trackable / not yet checked -->
  <span v-if="behindBy == null" class="text-meta text-ink-3" :title="mutedTitle">
    {{ compact ? '—' : 'Not tracked' }}
  </span>

  <!-- Up to date -->
  <StatusBadge v-else-if="behindBy === 0" status="ok" label="Up to date" />

  <!-- Behind by N (+ optional security emphasis) -->
  <span v-else class="inline-flex flex-nowrap items-center gap-1.5">
    <StatusBadge status="warn" :label="behindLabel" />
    <StatusBadge v-if="securityUpdate" status="err" label="Security" />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import StatusBadge from './StatusBadge.vue'

const props = withDefaults(
  defineProps<{
    behindBy: number | null | undefined
    latestRef?: string | null
    securityUpdate?: boolean
    /** Render the muted state as a bare "—" instead of "Not tracked". */
    compact?: boolean
  }>(),
  { securityUpdate: false, compact: false },
)

const behindLabel = computed(() =>
  props.behindBy === 1 ? '1 behind' : `${props.behindBy} behind`,
)

const mutedTitle = computed(() =>
  props.latestRef ? `Latest ${props.latestRef}` : 'Not tracked or not yet checked',
)
</script>
