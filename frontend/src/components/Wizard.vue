<template>
  <div class="rounded-lg border border-line bg-surface">
    <!-- Stepper header -->
    <ol class="flex items-center gap-1 overflow-x-auto border-b border-line px-5 py-3.5">
      <template v-for="(step, i) in steps" :key="step.key">
        <li class="flex shrink-0 items-center gap-2">
          <button
            type="button"
            class="fdm-focus flex items-center gap-2 rounded px-1 py-0.5 text-label transition"
            :class="[
              i === active ? 'font-semibold text-ink-1' : i < active ? 'text-ink-2' : 'text-ink-3',
              i < active ? 'cursor-pointer hover:text-ink-1' : 'cursor-default',
            ]"
            :disabled="i > active"
            :aria-current="i === active ? 'step' : undefined"
            @click="i < active && goTo(i)"
          >
            <span
              class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[11px] font-medium tabular-nums"
              :class="
                i < active
                  ? 'border-ink-1 bg-[var(--text-primary)] text-[var(--bg-base)]'
                  : i === active
                    ? 'border-line-strong text-ink-1'
                    : 'border-line text-ink-3'
              "
            >
              <LucideCheck v-if="i < active" class="h-3 w-3" />
              <template v-else>{{ i + 1 }}</template>
            </span>
            {{ step.label }}
          </button>
        </li>
        <li v-if="i < steps.length - 1" aria-hidden="true" class="h-px w-6 shrink-0 bg-[var(--border)]" />
      </template>
    </ol>

    <!-- Step bodies stay mounted (v-show) so going back never loses state -->
    <div class="px-5 py-4">
      <div v-for="(step, i) in steps" :key="step.key" v-show="i === active">
        <p v-if="step.description" class="mb-3 text-label text-ink-2">{{ step.description }}</p>
        <slot :name="`step-${step.key}`" :step="step" :index="i" />
      </div>
    </div>

    <!-- Footer -->
    <div class="flex items-center justify-between border-t border-line px-5 py-3.5">
      <Button
        variant="subtle"
        theme="gray"
        label="Back"
        :disabled="active === 0"
        @click="goTo(active - 1)"
      />
      <div class="flex items-center gap-3">
        <span class="text-meta tabular-nums text-ink-3">
          Step {{ active + 1 }} of {{ steps.length }}
        </span>
        <Button
          variant="solid"
          theme="gray"
          :label="isLast ? submitLabel : 'Continue'"
          :disabled="!canContinue"
          :loading="submitting"
          @click="next"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, ref, watch } from 'vue'
import LucideCheck from '~icons/lucide/check'
import type { WizardStep } from './types'

const props = withDefaults(
  defineProps<{
    steps: WizardStep[]
    /** v-model of the active step index (optional; the wizard self-manages otherwise). */
    modelValue?: number
    /** Verb on the final step's button, e.g. "Create bench" — never "Submit". */
    submitLabel: string
    /** Gate for the Continue/submit button; parent validates the current step. */
    canContinue?: boolean
    submitting?: boolean
  }>(),
  { canContinue: true, submitting: false },
)

const emit = defineEmits<{
  'update:modelValue': [index: number]
  submit: []
}>()

const internal = ref(props.modelValue ?? 0)
watch(
  () => props.modelValue,
  (v) => {
    if (v !== undefined) internal.value = v
  },
)

const active = computed(() => internal.value)
const isLast = computed(() => active.value === props.steps.length - 1)

function goTo(index: number) {
  if (index < 0 || index >= props.steps.length) return
  internal.value = index
  emit('update:modelValue', index)
}

function next() {
  if (!props.canContinue) return
  if (isLast.value) emit('submit')
  else goTo(active.value + 1)
}
</script>
