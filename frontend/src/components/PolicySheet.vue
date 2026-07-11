<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-150 ease-out"
      leave-to-class="opacity-0"
    >
      <div v-if="open" class="fixed inset-0 z-50 flex justify-end bg-black/60" @click.self="tryClose">
        <Transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="translate-x-full"
          leave-active-class="transition duration-150 ease-out"
          leave-to-class="translate-x-full"
          appear
        >
          <aside
            class="flex h-full w-full max-w-[520px] flex-col border-l border-line bg-base"
            role="dialog"
            aria-modal="true"
            :aria-label="`Backup policy for ${site?.name ?? 'site'}`"
          >
            <!-- Header -->
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">Backup policy</h2>
                <p class="text-meta text-ink-2">
                  Compliance targets for <span class="font-medium text-ink-1">{{ site?.name }}</span>.
                </p>
              </div>
              <button
                type="button"
                class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                aria-label="Close"
                @click="tryClose"
              >
                <LucideX class="h-4 w-4" />
              </button>
            </header>

            <!-- Body -->
            <div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
              <div v-if="loading" class="space-y-3">
                <div v-for="i in 4" :key="i" class="h-9 w-full animate-pulse rounded-lg bg-raised" />
              </div>

              <template v-else>
                <Field
                  label="RPO (hours)"
                  hint="Recovery point objective — a backup no older than this must exist. 1–8760."
                >
                  <input
                    v-model.number="form.rpo_hours"
                    v-bind="inputAttrs"
                    type="number"
                    min="1"
                    max="8760"
                    placeholder="24"
                  />
                </Field>

                <Field
                  label="Retention (days)"
                  hint="Keep backups at least this many days. Leave blank for no retention requirement."
                >
                  <div class="flex items-center gap-2">
                    <input
                      v-model.number="form.retention_days"
                      v-bind="inputAttrs"
                      type="number"
                      min="1"
                      placeholder="—"
                    />
                    <Button
                      v-if="form.retention_days != null"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Clear"
                      @click="form.retention_days = null"
                    />
                  </div>
                </Field>

                <label class="flex items-center gap-2.5">
                  <input
                    v-model="form.require_offsite"
                    type="checkbox"
                    class="h-4 w-4 rounded border-line accent-white"
                  />
                  <span class="text-label text-ink-1">Require offsite copy</span>
                  <span class="text-meta text-ink-3">(newest backup must be pushed to S3)</span>
                </label>

                <label class="flex items-center gap-2.5">
                  <input
                    v-model="form.require_restore_test"
                    type="checkbox"
                    class="h-4 w-4 rounded border-line accent-white"
                  />
                  <span class="text-label text-ink-1">Require restore test</span>
                  <span class="text-meta text-ink-3">(a backup must be restore-tested)</span>
                </label>

                <div class="border-t border-line pt-4">
                  <label class="flex items-center gap-2.5">
                    <input
                      v-model="form.enabled"
                      type="checkbox"
                      class="h-4 w-4 rounded border-line accent-white"
                    />
                    <span class="text-label text-ink-1">Policy enabled</span>
                    <span class="text-meta text-ink-3">(off = tracked but not evaluated)</span>
                  </label>
                </div>

                <p v-if="submitError" class="text-label text-err" role="alert">{{ submitError }}</p>
              </template>
            </div>

            <!-- Footer -->
            <footer class="flex items-center justify-between gap-2 border-t border-line px-5 py-3.5">
              <Button
                v-if="hasPolicy"
                variant="subtle"
                theme="red"
                label="Remove policy"
                :loading="removing"
                :disabled="submitting || loading"
                @click="remove"
              />
              <span v-else />
              <div class="flex gap-2">
                <Button variant="subtle" theme="gray" label="Cancel" :disabled="submitting || removing" @click="tryClose" />
                <Button
                  variant="solid"
                  theme="gray"
                  label="Save policy"
                  :loading="submitting"
                  :disabled="!canSubmit || loading"
                  @click="submit"
                />
              </div>
            </footer>
          </aside>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, reactive, ref, watch } from 'vue'
import LucideX from '~icons/lucide/x'
import { ApiError } from '../api/client'
import { complianceApi } from '../api/compliance'
import type { Site } from '../api/sites'
import Field from './SheetField.vue'
import { toast } from './toast'

const props = defineProps<{
  open: boolean
  /** The site whose policy is being edited. */
  site: Site | null
}>()
const emit = defineEmits<{ close: []; saved: [] }>()

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const loading = ref(false)
const submitting = ref(false)
const removing = ref(false)
const submitError = ref('')
/** True once a policy has been loaded for this site (enables Remove). */
const hasPolicy = ref(false)

const form = reactive({
  rpo_hours: 24 as number,
  retention_days: null as number | null,
  require_offsite: false,
  require_restore_test: false,
  enabled: true,
})

const canSubmit = computed(
  () => Number.isFinite(form.rpo_hours) && form.rpo_hours >= 1 && form.rpo_hours <= 8760,
)

async function loadPolicy() {
  const site = props.site
  if (!site) return
  loading.value = true
  submitError.value = ''
  hasPolicy.value = false
  // Defaults for a fresh policy.
  Object.assign(form, {
    rpo_hours: 24,
    retention_days: null,
    require_offsite: false,
    require_restore_test: false,
    enabled: true,
  })
  try {
    const p = await complianceApi.getPolicy(site.id)
    hasPolicy.value = true
    Object.assign(form, {
      rpo_hours: p.rpo_hours,
      retention_days: p.retention_days,
      require_offsite: p.require_offsite,
      require_restore_test: p.require_restore_test,
      enabled: p.enabled,
    })
  } catch (error) {
    // 404 = no policy yet; anything else is a real error worth surfacing.
    if (!(error instanceof ApiError && error.status === 404)) {
      submitError.value = error instanceof Error ? error.message : 'Could not load the policy.'
    }
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      submitting.value = false
      removing.value = false
      void loadPolicy()
    }
  },
)

function tryClose() {
  if (!submitting.value && !removing.value) emit('close')
}

async function submit() {
  if (submitting.value || !canSubmit.value || !props.site) return
  submitting.value = true
  submitError.value = ''
  try {
    await complianceApi.putPolicy(props.site.id, {
      rpo_hours: form.rpo_hours,
      retention_days: form.retention_days ?? null,
      require_offsite: form.require_offsite,
      require_restore_test: form.require_restore_test,
      enabled: form.enabled,
    })
    toast.success(`Policy saved for ${props.site.name}.`)
    emit('saved')
    emit('close')
  } catch (error) {
    submitError.value = error instanceof Error ? error.message : 'Could not save the policy.'
  } finally {
    submitting.value = false
  }
}

async function remove() {
  if (removing.value || !props.site) return
  removing.value = true
  submitError.value = ''
  try {
    await complianceApi.deletePolicy(props.site.id)
    toast.success(`Policy removed for ${props.site.name}.`)
    emit('saved')
    emit('close')
  } catch (error) {
    submitError.value = error instanceof Error ? error.message : 'Could not remove the policy.'
  } finally {
    removing.value = false
  }
}
</script>
