<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150 ease-out"
      leave-to-class="opacity-0"
    >
      <div
        v-if="modelValue"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
        @click.self="onOverlayClick"
        @keydown.esc="cancel"
      >
        <div
          ref="panel"
          role="dialog"
          aria-modal="true"
          :aria-label="title"
          tabindex="-1"
          class="fdm-focus w-full max-w-md rounded-lg border border-line bg-raised outline-none"
        >
          <!-- Header -->
          <div class="flex items-start gap-3 border-b border-line px-5 py-4">
            <div
              v-if="destructive"
              class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-err/10"
            >
              <LucideTriangleAlert class="text-err" style="height: 18px; width: 18px" />
            </div>
            <div class="min-w-0">
              <h2 class="text-section font-semibold" :class="destructive ? 'text-err' : 'text-ink-1'">
                {{ title }}
              </h2>
              <p v-if="message" class="mt-0.5 text-label text-ink-2">{{ message }}</p>
            </div>
          </div>

          <div class="max-h-[60vh] space-y-4 overflow-y-auto px-5 py-4">
            <slot />

            <!-- Plain-language consequence list (spec B5) -->
            <div v-if="consequences?.length || $slots.consequences">
              <p class="mb-1.5 text-meta font-medium uppercase tracking-wide text-ink-3">
                This will:
              </p>
              <slot name="consequences">
                <ul class="space-y-1">
                  <li
                    v-for="(line, i) in consequences"
                    :key="i"
                    class="flex items-start gap-2 text-label text-ink-1"
                  >
                    <LucideDot class="mt-0.5 h-4 w-4 shrink-0" :class="destructive ? 'text-err' : 'text-ink-3'" />
                    {{ line }}
                  </li>
                </ul>
              </slot>
            </div>

            <!-- What gets backed up first (spec: automatic pre-action backup) -->
            <div
              v-if="$slots.backup"
              class="flex items-start gap-2.5 rounded-lg border border-line bg-surface px-3 py-2.5"
            >
              <LucideArchive class="mt-0.5 h-4 w-4 shrink-0 text-ink-2" />
              <div class="text-label text-ink-2">
                <p class="font-medium text-ink-1">Backed up first, automatically</p>
                <slot name="backup" />
              </div>
            </div>

            <!-- Type-the-exact-name gate for destroy-class actions -->
            <div v-if="destructive && targetName">
              <label class="mb-1 block text-label text-ink-2">
                Type <span class="select-all font-mono font-semibold text-ink-1">{{ targetName }}</span> to
                confirm
              </label>
              <input
                ref="confirmInput"
                v-model="typed"
                type="text"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                :placeholder="targetName"
                class="fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 font-mono text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
              />
            </div>
          </div>

          <!-- Footer: cancel + a single verb button, red for destructive -->
          <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
            <Button variant="subtle" theme="gray" :label="cancelLabel" @click="cancel" />
            <Button
              :variant="'solid'"
              :theme="destructive ? 'red' : 'gray'"
              :label="verb"
              :disabled="!canConfirm"
              :loading="loading"
              @click="confirm"
            />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, nextTick, ref, watch } from 'vue'
import LucideArchive from '~icons/lucide/archive'
import LucideDot from '~icons/lucide/dot'
import LucideTriangleAlert from '~icons/lucide/triangle-alert'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    title: string
    /** One-line summary under the title. */
    message?: string
    /** The action verb on the confirm button, e.g. "Drop site dev.localhost" — never "OK". */
    verb: string
    variant?: 'standard' | 'destructive'
    /** Destroy-class actions: the exact name the user must type to arm the button. */
    targetName?: string
    /** Plain-language consequence lines (or use the #consequences slot). */
    consequences?: string[]
    cancelLabel?: string
    loading?: boolean
  }>(),
  { variant: 'standard', cancelLabel: 'Cancel' },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: []
  cancel: []
}>()

const destructive = computed(() => props.variant === 'destructive')

const typed = ref('')
const confirmInput = ref<HTMLInputElement | null>(null)
const panel = ref<HTMLElement | null>(null)

// The red verb button stays disabled until the typed name matches exactly.
const canConfirm = computed(() =>
  destructive.value && props.targetName ? typed.value === props.targetName : true,
)

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      typed.value = ''
      // Focus lands inside the dialog so Esc works immediately.
      nextTick(() => (confirmInput.value ?? panel.value)?.focus())
    }
  },
)

function onOverlayClick() {
  // Destroy-class modals must be dismissed deliberately, not by a stray click.
  if (!destructive.value) cancel()
}

function cancel() {
  emit('update:modelValue', false)
  emit('cancel')
}

function confirm() {
  if (!canConfirm.value) return
  emit('confirm')
}
</script>
