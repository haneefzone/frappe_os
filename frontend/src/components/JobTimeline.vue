<template>
  <ol class="space-y-0">
    <li v-for="(step, i) in steps" :key="i" class="relative flex gap-3 pb-5 last:pb-0">
      <!-- Connector line -->
      <span
        v-if="i < steps.length - 1"
        aria-hidden="true"
        class="absolute left-[9px] top-5 h-full w-px bg-[var(--border)]"
      />

      <!-- Marker: ○ pending · ◐ running · ● done · ✕ failed -->
      <span class="relative z-10 mt-0.5 flex h-[19px] w-[19px] shrink-0 items-center justify-center">
        <span
          v-if="step.status === 'pending'"
          class="h-3 w-3 rounded-full border-[1.5px] border-line-strong bg-surface"
        />
        <span
          v-else-if="step.status === 'running'"
          class="h-4 w-4 animate-spin rounded-full border-2 border-run/25 border-t-run"
          role="status"
          aria-label="Running"
        />
        <span
          v-else-if="step.status === 'done'"
          class="flex h-4 w-4 items-center justify-center rounded-full bg-ok"
        >
          <LucideCheck class="h-2.5 w-2.5 text-white" />
        </span>
        <span v-else class="flex h-4 w-4 items-center justify-center rounded-full bg-err">
          <LucideX class="h-2.5 w-2.5 text-white" />
        </span>
      </span>

      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-baseline gap-x-2">
          <span
            class="text-body"
            :class="{
              'text-ink-2': step.status === 'pending',
              'font-medium text-ink-1': step.status === 'running' || step.status === 'failed',
              'text-ink-1': step.status === 'done',
            }"
          >
            {{ step.label }}
          </span>
          <span v-if="step.status === 'running'" class="text-meta tabular-nums text-run">
            {{ elapsedFor(step) }}
          </span>
          <span v-else-if="step.status === 'done' && step.duration" class="text-meta tabular-nums text-ink-2">
            {{ step.duration }}
          </span>
          <span v-else-if="step.status === 'failed'" class="text-meta font-medium text-err">Failed</span>
        </div>

        <!-- Expandable error area for failed steps -->
        <template v-if="step.status === 'failed' && (step.error || $slots.error)">
          <button
            type="button"
            class="fdm-focus mt-1 flex items-center gap-1 rounded text-meta text-err transition hover:opacity-80"
            @click="toggleError(i)"
          >
            <LucideChevronRight
              class="h-3 w-3 transition-transform"
              :class="{ 'rotate-90': expanded.has(i) }"
            />
            {{ expanded.has(i) ? 'Hide error' : 'Show error' }}
          </button>
          <div
            v-if="expanded.has(i)"
            class="mt-1.5 overflow-x-auto rounded-lg border border-err/30 bg-err/5 px-3 py-2"
          >
            <slot name="error" :step="step" :index="i">
              <pre class="whitespace-pre-wrap font-mono text-meta leading-relaxed text-ink-1">{{ step.error }}</pre>
            </slot>
          </div>
        </template>
      </div>
    </li>
  </ol>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import LucideCheck from '~icons/lucide/check'
import LucideChevronRight from '~icons/lucide/chevron-right'
import LucideX from '~icons/lucide/x'
import type { JobStep } from './types'

const props = defineProps<{ steps: JobStep[] }>()

const expanded = ref(new Set<number>())

function toggleError(i: number) {
  const next = new Set(expanded.value)
  if (next.has(i)) next.delete(i)
  else next.add(i)
  expanded.value = next
}

// Live elapsed counter — ticks once a second, only while something is running.
const now = ref(Date.now())
let timer: number | undefined

const hasRunning = computed(() => props.steps.some((s) => s.status === 'running'))

function syncTimer() {
  if (hasRunning.value && timer === undefined) {
    timer = window.setInterval(() => (now.value = Date.now()), 1000)
  } else if (!hasRunning.value && timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

onMounted(syncTimer)
watch(hasRunning, syncTimer)
onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})

function elapsedFor(step: JobStep): string {
  if (!step.startedAt) return 'running…'
  const total = Math.max(0, Math.floor((now.value - step.startedAt) / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}
</script>
