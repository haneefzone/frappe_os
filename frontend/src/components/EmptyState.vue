<template>
  <div class="flex flex-col items-center justify-center px-6 py-16 text-center">
    <div
      v-if="icon"
      class="mb-4 flex h-12 w-12 items-center justify-center rounded-lg border border-line bg-raised"
    >
      <component :is="icon" class="h-5 w-5 text-ink-2" />
    </div>
    <h2 class="text-section font-semibold text-ink-1">
      {{ title }}
    </h2>
    <p class="mt-1 max-w-md text-body text-ink-2">
      {{ message }}
    </p>
    <div v-if="$slots.default || ctaLabel" class="mt-5">
      <slot>
        <Button variant="solid" theme="gray" :label="ctaLabel" @click="$emit('cta')" />
      </slot>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import type { FunctionalComponent, SVGAttributes } from 'vue'

defineProps<{
  title: string
  message: string
  icon?: FunctionalComponent<SVGAttributes>
  /** Primary call-to-action; a verb, e.g. "Add server". Or override via the default slot. */
  ctaLabel?: string
}>()

defineEmits<{ cta: [] }>()
</script>
