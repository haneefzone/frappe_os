<template>
  <label class="block">
    <span class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">{{ label }}</span>
    <slot />
    <span v-if="hint" class="mt-1 block text-meta text-ink-2">{{ hint }}</span>
  </label>
</template>

<script setup lang="ts">
defineProps<{ label: string; hint?: string }>()
</script>
