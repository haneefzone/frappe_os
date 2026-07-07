<template>
  <Teleport to="body">
    <div
      class="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 flex-col gap-2"
      aria-live="polite"
    >
      <TransitionGroup
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="translate-y-1 opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-for="item in toasts"
          :key="item.id"
          class="pointer-events-auto flex items-start gap-2.5 rounded-lg border border-line bg-raised px-3 py-2.5"
        >
          <component
            :is="ICONS[item.type]"
            class="mt-0.5 h-4 w-4 shrink-0"
            :style="{ color: COLORS[item.type] }"
          />
          <div class="min-w-0 flex-1">
            <p v-if="item.title" class="text-label font-semibold text-ink-1">{{ item.title }}</p>
            <p class="break-words text-label text-ink-2">{{ item.message }}</p>
          </div>
          <button
            type="button"
            class="fdm-focus -m-1 shrink-0 rounded p-1 text-ink-3 transition hover:text-ink-1"
            aria-label="Dismiss notification"
            @click="dismiss(item.id)"
          >
            <LucideX class="h-3.5 w-3.5" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import LucideCircleAlert from '~icons/lucide/circle-alert'
import LucideCircleCheck from '~icons/lucide/circle-check'
import LucideInfo from '~icons/lucide/info'
import LucideTriangleAlert from '~icons/lucide/triangle-alert'
import LucideX from '~icons/lucide/x'
import { dismiss, toasts, type ToastType } from './toast'

const ICONS = {
  info: LucideInfo,
  success: LucideCircleCheck,
  error: LucideCircleAlert,
  warning: LucideTriangleAlert,
} as const satisfies Record<ToastType, unknown>

const COLORS: Record<ToastType, string> = {
  info: '#3B82F6',
  success: '#22C55E',
  error: '#EF4444',
  warning: '#F59E0B',
}
</script>
