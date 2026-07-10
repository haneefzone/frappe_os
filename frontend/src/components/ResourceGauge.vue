<template>
  <div>
    <div v-if="label || showValue" class="mb-0.5 flex items-baseline justify-between gap-2">
      <span v-if="label" class="text-meta uppercase tracking-wide text-ink-3">{{ label }}</span>
      <span class="text-meta tabular-nums" :style="{ color: valueColor }">{{ valueLabel }}</span>
    </div>
    <div
      class="h-1.5 w-full overflow-hidden rounded-full bg-raised"
      role="progressbar"
      :aria-label="label || 'usage'"
      :aria-valuenow="clamped ?? undefined"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <div
        class="h-full rounded-full transition-all duration-150 ease-out"
        :style="{ width: `${clamped ?? 0}%`, background: barColor }"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { STATUS_COLOR } from './types'

const props = withDefaults(
  defineProps<{
    label?: string
    /** 0–100; null renders an empty muted bar with an em dash. */
    pct: number | null
    showValue?: boolean
  }>(),
  { showValue: true },
)

const clamped = computed(() => (props.pct == null ? null : Math.max(0, Math.min(100, props.pct))))

// Threshold discipline (spec B4.1): <70 ok, 70–90 warn, >90 err.
const tone = computed<'ok' | 'warn' | 'err' | 'muted'>(() => {
  if (clamped.value == null) return 'muted'
  if (clamped.value > 90) return 'err'
  if (clamped.value >= 70) return 'warn'
  return 'ok'
})

const barColor = computed(() => STATUS_COLOR[tone.value])
const valueColor = computed(() =>
  clamped.value == null ? 'var(--text-muted)' : STATUS_COLOR[tone.value],
)
const valueLabel = computed(() =>
  clamped.value == null ? '—' : `${Math.round(clamped.value)}%`,
)
</script>
