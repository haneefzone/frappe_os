<template>
  <div class="rounded-lg border border-line bg-surface p-4">
    <template v-if="loading">
      <div class="h-3 w-24 animate-pulse rounded bg-raised" />
      <div class="mt-3 h-7 w-16 animate-pulse rounded bg-raised" />
      <div class="mt-2 h-3 w-32 animate-pulse rounded bg-raised" />
    </template>
    <template v-else>
      <div class="flex items-center justify-between gap-2">
        <span class="text-meta font-medium uppercase tracking-wide text-ink-2">{{ label }}</span>
        <StatusDot v-if="status" :status="status" />
      </div>
      <div class="mt-1.5 text-kpi font-semibold tabular-nums text-ink-1">{{ value }}</div>
      <div v-if="delta || sublabel" class="mt-1 flex items-baseline gap-2 text-label">
        <span v-if="delta" class="font-medium tabular-nums" :style="{ color: deltaColor }">
          {{ delta }}
        </span>
        <span v-if="sublabel" class="text-ink-2">{{ sublabel }}</span>
      </div>
      <div v-if="$slots.footer" class="mt-3">
        <slot name="footer" />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import StatusDot from './StatusDot.vue'
import { STATUS_COLOR, type Status } from './types'

const props = defineProps<{
  label: string
  value?: string | number
  /** Small emphasized figure next to the sublabel, e.g. '+3' or '-12%'. */
  delta?: string
  /** Color of the delta figure; defaults to muted (monochrome discipline). */
  deltaTone?: 'ok' | 'warn' | 'err' | 'muted'
  sublabel?: string
  /** Status dot in the card corner. */
  status?: Status
  loading?: boolean
}>()

const deltaColor = computed(() =>
  props.deltaTone && props.deltaTone !== 'muted'
    ? STATUS_COLOR[props.deltaTone]
    : 'var(--text-secondary)',
)
</script>
