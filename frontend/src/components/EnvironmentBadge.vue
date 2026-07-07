<template>
  <span
    class="inline-flex items-center rounded border px-1.5 py-px text-[11px] font-semibold uppercase tracking-wider"
    :class="styles[env]"
  >
    {{ env === 'prod' ? 'PROD' : env === 'staging' ? 'STAGING' : 'DEV' }}
  </span>
</template>

<script setup lang="ts">
import type { Environment } from './types'

defineProps<{ env: Environment }>()

// Spec B2: PROD red-outline / STAGING amber / DEV grey.
const styles: Record<Environment, string> = {
  prod: 'border-err/60 text-err bg-transparent',
  staging: 'border-warn/40 text-warn bg-warn/10',
  dev: 'border-line-strong text-ink-2 bg-transparent',
}
</script>
