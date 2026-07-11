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
            :aria-label="editing ? 'Edit storage target' : 'Add storage target'"
          >
            <!-- Header -->
            <header class="flex items-center justify-between border-b border-line px-5 py-4">
              <div>
                <h2 class="text-section font-semibold text-ink-1">
                  {{ editing ? 'Edit storage target' : 'Add storage target' }}
                </h2>
                <p class="text-meta text-ink-2">
                  An S3-compatible bucket backup artifacts are pushed to for offsite retention.
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
              <Field label="Name" hint="A label for this target, e.g. “Offsite (Backblaze)”.">
                <input v-model.trim="form.name" v-bind="inputAttrs" placeholder="Offsite backups" />
              </Field>

              <Field label="Provider" hint="Drives the badge only; any S3-compatible service works.">
                <select v-model="form.provider" v-bind="inputAttrs">
                  <option v-for="p in providers" :key="p.value" :value="p.value">{{ p.label }}</option>
                </select>
              </Field>

              <Field
                label="Endpoint URL"
                hint="Leave blank for AWS S3 (derived from the region). Required for MinIO / B2 / Wasabi."
              >
                <input
                  v-model.trim="form.endpoint_url"
                  v-bind="inputAttrs"
                  class="font-mono"
                  placeholder="https://s3.eu-central-003.backblazeb2.com"
                />
              </Field>

              <div class="grid grid-cols-2 gap-3">
                <Field label="Bucket">
                  <input v-model.trim="form.bucket" v-bind="inputAttrs" placeholder="my-fdm-backups" />
                </Field>
                <Field label="Region" hint="Optional for non-AWS.">
                  <input v-model.trim="form.region" v-bind="inputAttrs" placeholder="us-east-1" />
                </Field>
              </div>

              <Field
                label="Path prefix"
                hint="Optional key prefix so one bucket can hold several platforms/tenants."
              >
                <input v-model.trim="form.path_prefix" v-bind="inputAttrs" class="font-mono" placeholder="fdm/prod" />
              </Field>

              <!-- Credentials — write-only (Fernet at rest, never returned) -->
              <div class="rounded-lg border border-line bg-surface p-3">
                <div class="mb-2 flex items-center gap-2">
                  <LucideKeyRound class="h-3.5 w-3.5 text-ink-3" />
                  <span class="text-label font-medium text-ink-1">S3 credentials</span>
                  <span v-if="editing && keysSet" class="ml-auto text-meta text-ink-3">Keys are set</span>
                </div>
                <Field label="Access key ID">
                  <input
                    v-model.trim="form.access_key"
                    v-bind="inputAttrs"
                    autocomplete="off"
                    :placeholder="editing && keysSet ? 'Leave blank to keep the current key' : 'AKIA…'"
                  />
                </Field>
                <Field label="Secret access key" class="mt-3">
                  <input
                    v-model="form.secret_key"
                    v-bind="inputAttrs"
                    type="password"
                    autocomplete="new-password"
                    :placeholder="editing && keysSet ? 'Leave blank to keep the current key' : '••••••••••••••••'"
                  />
                </Field>
                <p class="mt-2 text-meta text-ink-3">
                  Stored encrypted (Fernet) and never shown again. They never leave the server —
                  offsite downloads use short-lived presigned URLs.
                </p>
              </div>

              <div class="flex flex-wrap gap-4">
                <label class="flex items-center gap-2 text-label text-ink-1">
                  <input v-model="form.use_ssl" type="checkbox" class="accent-white" />
                  Use TLS (https)
                </label>
                <label class="flex items-center gap-2 text-label text-ink-1">
                  <input v-model="form.enabled" type="checkbox" class="accent-white" />
                  Enabled
                </label>
              </div>

              <!-- Test connection (only meaningful once the target is saved) -->
              <div v-if="editing" class="rounded-lg border border-line bg-surface p-3">
                <div class="flex items-center justify-between">
                  <span class="text-label font-medium text-ink-1">Test connection</span>
                  <Button
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    :label="testing ? 'Testing…' : 'Test connection'"
                    :loading="testing"
                    @click="runTest"
                  />
                </div>
                <p
                  v-if="testResult"
                  class="mt-2 rounded-md px-3 py-2 text-label"
                  :class="testResult.reachable && testResult.writable
                    ? 'border border-ok/40 bg-ok/10 text-ok'
                    : testResult.reachable
                      ? 'border border-warn/40 bg-warn/10 text-warn'
                      : 'border border-err/40 bg-err/10 text-err'"
                  role="status"
                >
                  {{ testSummary }}
                </p>
                <p class="mt-2 text-meta text-ink-3">Save your changes first — the test uses the stored keys.</p>
              </div>

              <p v-if="submitError" class="text-label text-err" role="alert">{{ submitError }}</p>
            </div>

            <!-- Footer -->
            <footer class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" @click="tryClose" />
              <Button
                variant="solid"
                theme="gray"
                :label="editing ? 'Save target' : 'Add target'"
                :loading="submitting"
                :disabled="!canSubmit"
                @click="submit"
              />
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
import LucideKeyRound from '~icons/lucide/key-round'
import LucideX from '~icons/lucide/x'
import { ApiError } from '../api/client'
import {
  STORAGE_PROVIDER_LABEL,
  type StorageProvider,
  type StorageTarget,
  type TestConnectionResult,
  storageApi,
} from '../api/storage'
import { toast } from './toast'
import Field from './SheetField.vue'

const props = defineProps<{
  open: boolean
  /** When set, the sheet edits an existing target instead of creating one. */
  target?: StorageTarget | null
}>()
const emit = defineEmits<{ close: []; saved: [] }>()

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const providers = (Object.keys(STORAGE_PROVIDER_LABEL) as StorageProvider[]).map((value) => ({
  value,
  label: STORAGE_PROVIDER_LABEL[value],
}))

const editing = computed(() => props.target != null)
const keysSet = computed(() => props.target?.keys_set ?? false)

const form = reactive({
  name: '',
  provider: 'aws' as StorageProvider,
  endpoint_url: '',
  region: '',
  bucket: '',
  path_prefix: '',
  access_key: '',
  secret_key: '',
  use_ssl: true,
  enabled: true,
})

const submitting = ref(false)
const submitError = ref('')
const testing = ref(false)
const testResult = ref<TestConnectionResult | null>(null)

// Creating a usable target needs both keys; editing keeps them if left blank.
const canSubmit = computed(() => {
  const base = form.name.length > 0 && form.bucket.length > 0
  if (editing.value) return base
  return base && form.access_key.length > 0 && form.secret_key.length > 0
})

const testSummary = computed(() => {
  const r = testResult.value
  if (!r) return ''
  if (!r.reachable) return r.error ?? 'Bucket not reachable.'
  const latency = r.latency_ms != null ? ` (${r.latency_ms} ms)` : ''
  if (!r.writable) return `Reachable but not writable${latency}: ${r.error ?? 'write denied'}.`
  return `Reachable and writable${latency}.`
})

function reset() {
  submitError.value = ''
  submitting.value = false
  testing.value = false
  testResult.value = null
  const t = props.target
  Object.assign(form, {
    name: t?.name ?? '',
    provider: (t?.provider as StorageProvider) ?? 'aws',
    endpoint_url: t?.endpoint_url ?? '',
    region: t?.region ?? '',
    bucket: t?.bucket ?? '',
    path_prefix: t?.path_prefix ?? '',
    access_key: '',
    secret_key: '',
    use_ssl: t?.use_ssl ?? true,
    enabled: t?.enabled ?? true,
  })
}

watch(
  () => props.open,
  (open) => {
    if (open) reset()
  },
)

async function submit() {
  if (submitting.value || !canSubmit.value) return
  submitting.value = true
  submitError.value = ''
  try {
    const common = {
      name: form.name,
      provider: form.provider,
      endpoint_url: form.endpoint_url || null,
      region: form.region || null,
      bucket: form.bucket,
      path_prefix: form.path_prefix || null,
      use_ssl: form.use_ssl,
      enabled: form.enabled,
    }
    if (props.target) {
      // Keys are write-only: only send them when the user typed a new value.
      await storageApi.update(props.target.id, {
        ...common,
        ...(form.access_key ? { access_key: form.access_key } : {}),
        ...(form.secret_key ? { secret_key: form.secret_key } : {}),
      })
    } else {
      await storageApi.create({ ...common, access_key: form.access_key, secret_key: form.secret_key })
    }
    emit('saved')
    emit('close')
  } catch (error) {
    submitError.value =
      error instanceof ApiError && error.status === 409
        ? `A storage target named "${form.name}" already exists.`
        : error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Could not save the storage target.'
  } finally {
    submitting.value = false
  }
}

async function runTest() {
  if (testing.value || !props.target) return
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await storageApi.testConnection(props.target.id)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not run the connection test.')
  } finally {
    testing.value = false
  }
}

function tryClose() {
  if (!submitting.value) emit('close')
}
</script>
