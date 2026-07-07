<template>
  <div class="flex flex-col items-center justify-center px-6 py-16 text-center">
    <div
      v-if="icon"
      class="mb-4 flex h-12 w-12 items-center justify-center rounded-lg border"
      :style="{ background: 'var(--bg-raised)', borderColor: 'var(--border)' }"
    >
      <component :is="icon" class="h-5 w-5" :style="{ color: 'var(--text-secondary)' }" />
    </div>
    <h2 class="text-base font-semibold" :style="{ color: 'var(--text-primary)' }">
      {{ title }}
    </h2>
    <p class="mt-1 max-w-md text-sm" :style="{ color: 'var(--text-secondary)' }">
      {{ message }}
    </p>
    <div v-if="$slots.default" class="mt-5">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { FunctionalComponent, SVGAttributes } from 'vue'

defineProps<{
  title: string
  message: string
  icon?: FunctionalComponent<SVGAttributes>
}>()
</script>
