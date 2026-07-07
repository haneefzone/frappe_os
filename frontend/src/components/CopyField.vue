<template>
  <div>
    <label v-if="label" class="mb-1 block text-meta font-medium text-ink-2">{{ label }}</label>
    <div class="flex items-center gap-1 rounded-lg border border-line bg-surface px-2.5 py-1.5">
      <span
        class="min-w-0 flex-1 truncate text-label text-ink-1"
        :class="{ 'font-mono': mono }"
        :title="displayValue"
      >
        {{ displayValue }}
      </span>
      <button
        v-if="secret"
        type="button"
        class="fdm-focus shrink-0 rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
        :aria-label="revealed ? 'Hide value' : 'Reveal value'"
        @click="revealed = !revealed"
      >
        <LucideEyeOff v-if="revealed" class="h-3.5 w-3.5" />
        <LucideEye v-else class="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        class="fdm-focus shrink-0 rounded p-1 transition hover:bg-raised"
        :class="copied ? 'text-ok' : 'text-ink-3 hover:text-ink-1'"
        aria-label="Copy to clipboard"
        @click="copy"
      >
        <LucideCheck v-if="copied" class="h-3.5 w-3.5" />
        <LucideCopy v-else class="h-3.5 w-3.5" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import LucideCheck from '~icons/lucide/check'
import LucideCopy from '~icons/lucide/copy'
import LucideEye from '~icons/lucide/eye'
import LucideEyeOff from '~icons/lucide/eye-off'
import { toast } from './toast'

const props = withDefaults(
  defineProps<{
    value: string
    label?: string
    /** Mask the value with dots until revealed; copy still copies the real value. */
    secret?: boolean
    mono?: boolean
  }>(),
  { secret: false, mono: true },
)

const revealed = ref(false)
const copied = ref(false)

// Fixed-length mask so the display never leaks the secret's length.
const displayValue = computed(() =>
  props.secret && !revealed.value ? '••••••••••••' : props.value,
)

async function copy() {
  try {
    await navigator.clipboard.writeText(props.value)
    copied.value = true
    window.setTimeout(() => (copied.value = false), 1500)
  } catch {
    toast.error('Could not copy to clipboard — copy it manually instead.')
  }
}
</script>
