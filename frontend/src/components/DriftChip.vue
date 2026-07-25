<template>
  <button
    type="button"
    class="fdm-focus inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-meta font-medium transition hover:opacity-80"
    :style="chipStyle"
    :title="`${baseline.path} — ${statusLabel}`"
    @click.stop="$emit('click')"
  >
    <StatusDot :status="dot" size="sm" />
    <span class="max-w-[160px] truncate">{{ baseline.artifact_key }}</span>
    <span class="opacity-70">{{ statusLabel }}</span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import StatusDot from './StatusDot.vue'
import { STATUS_COLOR, type Status } from './types'
import type { DriftBaseline, DriftStatus } from '../api/drift'

const props = defineProps<{ baseline: DriftBaseline }>()
defineEmits<{ click: [] }>()

const STATUS_DOT: Record<DriftStatus, Status> = {
  baseline: 'ok',
  drifted: 'err',
  accepted: 'warn',
}

const STATUS_LABEL: Record<DriftStatus, string> = {
  baseline: 'in sync',
  drifted: 'drifted',
  accepted: 'accepted',
}

const dot = computed(() => STATUS_DOT[props.baseline.status as DriftStatus] ?? 'muted')
const statusLabel = computed(() => STATUS_LABEL[props.baseline.status as DriftStatus] ?? props.baseline.status)

const chipStyle = computed(() => {
  const color = STATUS_COLOR[dot.value]
  return {
    color,
    borderColor: `color-mix(in srgb, ${color} 35%, transparent)`,
    background: `color-mix(in srgb, ${color} 10%, transparent)`,
  }
})
</script>
