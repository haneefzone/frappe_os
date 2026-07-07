<template>
  <span
    class="inline-block shrink-0 rounded-full"
    :class="[sizeClass, { 'fdm-pulse': shouldPulse }]"
    :style="{ background: STATUS_COLOR[status] }"
    role="img"
    :aria-label="ariaLabel ?? status"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { STATUS_COLOR, type Status } from './types'

const props = withDefaults(
  defineProps<{
    status: Status
    size?: 'sm' | 'md'
    /** Defaults to pulsing only while running. */
    pulse?: boolean
    ariaLabel?: string
  }>(),
  { size: 'md' },
)

const sizeClass = computed(() => (props.size === 'sm' ? 'h-1.5 w-1.5' : 'h-2 w-2'))
const shouldPulse = computed(() => props.pulse ?? props.status === 'running')
</script>
