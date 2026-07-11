<template>
  <span
    class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-meta font-medium whitespace-nowrap"
    :style="{
      color: STATUS_COLOR[status],
      borderColor: `color-mix(in srgb, ${STATUS_COLOR[status]} 35%, transparent)`,
      background: `color-mix(in srgb, ${STATUS_COLOR[status]} 10%, transparent)`,
    }"
  >
    <StatusDot :status="status" size="sm" />
    {{ label ?? DEFAULT_LABEL[status] }}
  </span>
</template>

<script setup lang="ts">
import StatusDot from './StatusDot.vue'
import { STATUS_COLOR, type Status } from './types'

defineProps<{
  status: Status
  label?: string
}>()

const DEFAULT_LABEL: Record<Status, string> = {
  ok: 'Healthy',
  warn: 'Warning',
  err: 'Error',
  running: 'Running',
  muted: 'Inactive',
}
</script>
