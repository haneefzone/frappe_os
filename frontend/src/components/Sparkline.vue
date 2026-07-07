<template>
  <svg
    :width="width"
    :height="height"
    :viewBox="`0 0 ${width} ${height}`"
    fill="none"
    aria-hidden="true"
    class="block"
  >
    <polygon v-if="filled" :points="areaPoints" :fill="color" opacity="0.12" />
    <polyline
      :points="linePoints"
      :stroke="color"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
    <circle v-if="showLast && coords.length" :cx="lastPoint.x" :cy="lastPoint.y" r="2" :fill="color" />
  </svg>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { STATUS_COLOR, type Status } from './types'

const props = withDefaults(
  defineProps<{
    data: number[]
    /** Monochrome by default; pass a status to accent it (spec B2). 'info' = the run/info blue. */
    status?: Exclude<Status, 'muted'> | 'info'
    width?: number
    height?: number
    filled?: boolean
    /** Emphasize the most recent point. */
    showLast?: boolean
  }>(),
  { width: 120, height: 32, filled: false, showLast: false },
)

const PAD = 2

const coords = computed(() => {
  const n = props.data.length
  if (n === 0) return []
  const min = Math.min(...props.data)
  const max = Math.max(...props.data)
  const span = max - min || 1
  const innerW = props.width - PAD * 2
  const innerH = props.height - PAD * 2
  return props.data.map((v, i) => ({
    x: PAD + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW),
    y: PAD + innerH - ((v - min) / span) * innerH,
  }))
})

const linePoints = computed(() => coords.value.map((p) => `${p.x},${p.y}`).join(' '))

const areaPoints = computed(() => {
  if (!coords.value.length) return ''
  const first = coords.value[0]
  const last = coords.value[coords.value.length - 1]
  const bottom = props.height - PAD
  return `${first.x},${bottom} ${linePoints.value} ${last.x},${bottom}`
})

const lastPoint = computed(() => coords.value[coords.value.length - 1])

const color = computed(() =>
  props.status
    ? STATUS_COLOR[props.status === 'info' ? 'running' : props.status]
    : 'var(--text-muted)',
)
</script>
