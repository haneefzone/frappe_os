<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Settings</h1>
        <p class="text-meta text-ink-2">White-label branding, platform defaults, and environment.</p>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <!-- Tabs -->
      <div class="mb-6 flex gap-1 border-b border-line">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          class="fdm-focus -mb-px border-b-2 px-3 py-2 text-label font-medium transition-colors"
          :class="tab === t.key ? 'border-ink-1 text-ink-1' : 'border-transparent text-ink-3 hover:text-ink-1'"
          @click="tab = t.key"
        >
          {{ t.label }}
        </button>
      </div>

      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <div v-if="loading" class="max-w-2xl space-y-4">
        <div v-for="i in 4" :key="i" class="h-16 animate-pulse rounded-lg border border-line bg-surface" />
      </div>

      <template v-else-if="settings">
        <p v-if="!canManage" class="mb-4 rounded-lg border border-line bg-surface px-4 py-2.5 text-label text-ink-2">
          You have read-only access to settings. Ask an Admin to make changes.
        </p>

        <!-- General: white-label branding -->
        <section v-show="tab === 'general'" class="max-w-2xl space-y-6">
          <div class="rounded-lg border border-line bg-surface p-5">
            <h2 class="text-section font-semibold text-ink-1">Branding</h2>
            <p class="mt-0.5 text-label text-ink-2">
              This is the white-label layer — the product name and logo shown in the sidebar and
              on the sign-in page.
            </p>

            <div class="mt-4 space-y-4">
              <div>
                <label for="product-name" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Product name
                </label>
                <input
                  id="product-name"
                  v-model="general.productName"
                  type="text"
                  :disabled="!canManage"
                  v-bind="inputAttrs"
                />
              </div>

              <div>
                <span class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">Logo</span>
                <div class="flex items-center gap-4">
                  <div
                    class="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-md border border-line-strong bg-base"
                  >
                    <img v-if="logoPreview" :src="logoPreview" alt="Logo preview" class="h-full w-full object-contain" />
                    <span v-else class="text-lg font-bold text-ink-1">{{ (general.productName || 'F').charAt(0).toUpperCase() }}</span>
                  </div>
                  <div class="space-y-1">
                    <input
                      ref="fileInput"
                      type="file"
                      accept="image/png,image/jpeg,image/svg+xml,image/webp"
                      class="hidden"
                      @change="onLogoPicked"
                    />
                    <Button
                      v-if="canManage"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      :label="uploadingLogo ? 'Uploading…' : 'Upload logo'"
                      :loading="uploadingLogo"
                      @click="fileInput?.click()"
                    />
                    <p class="text-meta text-ink-3">PNG, JPEG, SVG, or WebP. Max 512&nbsp;KB.</p>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="canManage" class="mt-5 flex items-center gap-3">
              <Button
                variant="solid"
                theme="gray"
                :label="savingGeneral ? 'Saving…' : 'Save settings'"
                :loading="savingGeneral"
                @click="saveGeneral"
              />
            </div>
          </div>
        </section>

        <!-- Defaults -->
        <section v-show="tab === 'defaults'" class="max-w-2xl space-y-6">
          <div class="rounded-lg border border-line bg-surface p-5">
            <h2 class="text-section font-semibold text-ink-1">Platform defaults</h2>
            <p class="mt-0.5 text-label text-ink-2">Where new benches live and the port range they draw from.</p>

            <div class="mt-4 space-y-4">
              <div>
                <label for="default-tz" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Default timezone
                </label>
                <input id="default-tz" v-model="defaults.defaultTz" type="text" :disabled="!canManage" v-bind="inputAttrs" />
              </div>
              <div>
                <label for="bench-base" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                  Bench base path
                </label>
                <input
                  id="bench-base"
                  v-model="defaults.benchBasePath"
                  type="text"
                  placeholder="/home/frappe/benches"
                  :disabled="!canManage"
                  v-bind="inputAttrs"
                  class="font-mono"
                />
                <p class="mt-1 text-meta text-ink-3">Must be an absolute path.</p>
              </div>
              <div class="grid grid-cols-2 gap-4">
                <div>
                  <label for="port-start" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                    Port range start
                  </label>
                  <input id="port-start" v-model.number="defaults.portStart" type="number" :disabled="!canManage" v-bind="inputAttrs" />
                </div>
                <div>
                  <label for="port-end" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                    Port range end
                  </label>
                  <input id="port-end" v-model.number="defaults.portEnd" type="number" :disabled="!canManage" v-bind="inputAttrs" />
                </div>
              </div>
              <p v-if="defaultsError" class="text-label text-err" role="alert">{{ defaultsError }}</p>
            </div>

            <div v-if="canManage" class="mt-5">
              <Button
                variant="solid"
                theme="gray"
                :label="savingDefaults ? 'Saving…' : 'Save settings'"
                :loading="savingDefaults"
                @click="saveDefaults"
              />
            </div>
          </div>
        </section>

        <!-- Storage: S3-compatible offsite targets (session 2.2) -->
        <section v-show="tab === 'storage'" class="max-w-3xl space-y-5">
          <div class="rounded-lg border border-line bg-surface">
            <div class="flex items-center justify-between border-b border-line px-5 py-3.5">
              <div>
                <h2 class="text-section font-semibold text-ink-1">Offsite storage</h2>
                <p class="mt-0.5 text-label text-ink-2">
                  S3-compatible buckets backup artifacts are pushed to, with checksum re-verification.
                </p>
              </div>
              <Button v-if="canManage" variant="solid" theme="gray" size="sm" label="Add target" @click="openTargetSheet(null)">
                <template #prefix><LucidePlus class="h-4 w-4" /></template>
              </Button>
            </div>

            <p v-if="storageError" class="px-5 py-3 text-label text-err" role="alert">{{ storageError }}</p>

            <div v-if="storageLoading" class="p-5">
              <div v-for="i in 2" :key="i" class="mb-2 h-12 w-full animate-pulse rounded bg-raised" />
            </div>

            <EmptyState
              v-else-if="targets.length === 0"
              :icon="LucideCloud"
              title="No storage targets"
              message="Add an S3-compatible bucket to push backups offsite."
              :cta-label="canManage ? 'Add target' : undefined"
              @cta="openTargetSheet(null)"
            />

            <ul v-else class="divide-y divide-line">
              <li v-for="t in targets" :key="t.id" class="flex items-center gap-4 px-5 py-3.5">
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <span class="truncate font-medium text-ink-1">{{ t.name }}</span>
                    <!-- Provider is a neutral label; enabled/disabled is a separate state badge (F1/F2). -->
                    <StatusBadge status="muted" :label="providerLabel(t.provider)" />
                    <StatusBadge :status="t.enabled ? 'ok' : 'muted'" :label="t.enabled ? 'Enabled' : 'Disabled'" />
                    <StatusBadge v-if="!t.keys_set" status="warn" label="No keys" />
                  </div>
                  <p class="mt-0.5 truncate font-mono text-meta text-ink-3">
                    {{ t.bucket }}<span v-if="t.path_prefix">/{{ t.path_prefix }}</span>
                    <span v-if="t.endpoint_url"> · {{ t.endpoint_url }}</span>
                  </p>
                  <p
                    v-if="testResults[t.id]"
                    class="mt-1 text-meta"
                    :class="testTone(testResults[t.id])"
                    role="status"
                  >
                    {{ testLine(testResults[t.id]) }}
                  </p>
                </div>
                <div v-if="canManage" class="flex items-center gap-1">
                  <Button
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    :label="testingId === t.id ? 'Testing…' : 'Test'"
                    :loading="testingId === t.id"
                    @click="testTarget(t)"
                  />
                  <Button variant="subtle" theme="gray" size="sm" label="Edit" @click="openTargetSheet(t)" />
                  <Button variant="subtle" theme="gray" size="sm" label="Delete" @click="confirmDelete(t)" />
                </div>
              </li>
            </ul>
          </div>
        </section>

        <!-- Environment (read-only) -->
        <section v-show="tab === 'environment'" class="max-w-2xl">
          <div class="rounded-lg border border-line bg-surface">
            <h2 class="border-b border-line px-5 py-3 text-section font-semibold text-ink-1">Environment</h2>
            <div v-if="!environment" class="p-5">
              <div v-for="i in 6" :key="i" class="mb-2 h-4 w-full animate-pulse rounded bg-raised" />
            </div>
            <dl v-else class="divide-y divide-line">
              <div v-for="row in environmentRows" :key="row.label" class="flex items-center justify-between px-5 py-2.5">
                <dt class="text-meta uppercase tracking-wide text-ink-3">{{ row.label }}</dt>
                <dd class="max-w-[60%] truncate text-label text-ink-1" :title="row.value">{{ row.value }}</dd>
              </div>
            </dl>
          </div>
        </section>
      </template>
    </div>

    <StorageTargetSheet
      :open="targetSheetOpen"
      :target="editingTarget"
      @close="targetSheetOpen = false"
      @saved="onTargetSaved"
    />

    <ConfirmModal
      :model-value="deleteTarget != null"
      title="Delete storage target"
      :message="deleteTarget ? `Remove “${deleteTarget.name}”?` : ''"
      verb="Delete target"
      variant="destructive"
      :consequences="[
        'Existing offsite backups keep their records but can no longer be downloaded through this target.',
      ]"
      :loading="deleting"
      @confirm="doDelete"
      @cancel="deleteTarget = null"
      @update:model-value="(v: boolean) => { if (!v) deleteTarget = null }"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import LucideCloud from '~icons/lucide/cloud'
import LucidePlus from '~icons/lucide/plus'
import { ApiError } from '../api/client'
import {
  type Environment,
  type LogoContentType,
  type Settings,
  settingsApi,
} from '../api/settings'
import {
  STORAGE_PROVIDER_LABEL,
  type StorageTarget,
  type TestConnectionResult,
  storageApi,
} from '../api/storage'
import ConfirmModal from '../components/ConfirmModal.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StorageTargetSheet from '../components/StorageTargetSheet.vue'
import { toast } from '../components/toast'
import { useAuthStore } from '../stores/auth'
import { useSettingsStore } from '../stores/settings'

const auth = useAuthStore()
const settingsStore = useSettingsStore()
const canManage = auth.hasPermission('settings:manage')

type TabKey = 'general' | 'defaults' | 'storage' | 'environment'
const tabs = computed<{ key: TabKey; label: string }[]>(() => [
  { key: 'general', label: 'General' },
  { key: 'defaults', label: 'Defaults' },
  // Storage-target endpoints are Admin-only (settings:manage); hide the tab
  // entirely for read-only users since even listing requires that permission.
  ...(canManage ? ([{ key: 'storage', label: 'Storage' }] as const) : []),
  { key: 'environment', label: 'Environment' },
])
const tab = ref<TabKey>('general')

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong disabled:opacity-60',
}

const settings = ref<Settings | null>(null)
const environment = ref<Environment | null>(null)
const loading = ref(true)
const loadError = ref('')

const general = reactive({ productName: '' })
const defaults = reactive({ defaultTz: '', benchBasePath: '', portStart: 0, portEnd: 0 })

const savingGeneral = ref(false)
const savingDefaults = ref(false)
const defaultsError = ref('')

// Logo preview: a freshly picked data URL, else the persisted logo, else none.
const pickedPreview = ref<string | null>(null)
const uploadingLogo = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const logoPreview = computed(() => {
  if (pickedPreview.value) return pickedPreview.value
  const s = settings.value
  return s?.logo_path ? settingsApi.logoUrl(s.updated_at) : null
})

const environmentRows = computed(() => {
  const e = environment.value
  if (!e) return []
  return [
    { label: 'App version', value: e.app_version },
    { label: 'Python', value: e.python_version },
    { label: 'Platform', value: e.platform },
    { label: 'Database', value: e.database_backend },
    { label: 'Redis', value: e.redis_configured ? 'Configured' : 'Not configured' },
    { label: 'Debug', value: e.debug ? 'On' : 'Off' },
    { label: 'Default timezone', value: e.default_tz },
    {
      label: 'Monitoring',
      value: e.monitoring_enabled ? `Every ${e.monitoring_interval_seconds}s` : 'Disabled',
    },
  ]
})

function apply(s: Settings) {
  settings.value = s
  general.productName = s.product_name
  defaults.defaultTz = s.default_tz
  defaults.benchBasePath = s.bench_base_path
  defaults.portStart = s.port_range_start
  defaults.portEnd = s.port_range_end
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [s, e] = await Promise.all([settingsApi.get(), settingsApi.environment()])
    apply(s)
    environment.value = e
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load settings.'
  } finally {
    loading.value = false
  }
}

async function saveGeneral() {
  if (savingGeneral.value) return
  savingGeneral.value = true
  try {
    const updated = await settingsApi.update({ product_name: general.productName.trim() })
    apply(updated)
    settingsStore.set(updated)
    toast.success('Settings saved.')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not save settings.')
  } finally {
    savingGeneral.value = false
  }
}

async function saveDefaults() {
  if (savingDefaults.value) return
  defaultsError.value = ''
  if (defaults.portStart > defaults.portEnd) {
    defaultsError.value = 'Port range start must be less than or equal to the end.'
    return
  }
  if (!defaults.benchBasePath.startsWith('/')) {
    defaultsError.value = 'Bench base path must be absolute (start with “/”).'
    return
  }
  savingDefaults.value = true
  try {
    const updated = await settingsApi.update({
      default_tz: defaults.defaultTz.trim(),
      bench_base_path: defaults.benchBasePath.trim(),
      port_range_start: defaults.portStart,
      port_range_end: defaults.portEnd,
    })
    apply(updated)
    settingsStore.set(updated)
    toast.success('Settings saved.')
  } catch (error) {
    if (error instanceof ApiError && error.status === 422) {
      defaultsError.value = error.message
    } else {
      toast.error(error instanceof Error ? error.message : 'Could not save settings.')
    }
  } finally {
    savingDefaults.value = false
  }
}

const LOGO_TYPES: Record<string, LogoContentType> = {
  'image/png': 'image/png',
  'image/jpeg': 'image/jpeg',
  'image/svg+xml': 'image/svg+xml',
  'image/webp': 'image/webp',
}

function onLogoPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // allow re-picking the same file
  if (!file) return
  const contentType = LOGO_TYPES[file.type]
  if (!contentType) {
    toast.error('Logo must be a PNG, JPEG, SVG, or WebP image.')
    return
  }
  if (file.size > 512 * 1024) {
    toast.error('Logo must be 512 KB or smaller.')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    const dataUrl = reader.result as string
    pickedPreview.value = dataUrl
    void uploadLogo(contentType, dataUrl)
  }
  reader.onerror = () => toast.error('Could not read the selected file.')
  reader.readAsDataURL(file)
}

async function uploadLogo(contentType: LogoContentType, dataUrl: string) {
  uploadingLogo.value = true
  try {
    // Backend tolerates the data: prefix, but strip it for a clean base64 payload.
    const base64 = dataUrl.includes(',') ? dataUrl.slice(dataUrl.indexOf(',') + 1) : dataUrl
    const updated = await settingsApi.uploadLogo({ content_type: contentType, content_base64: base64 })
    apply(updated)
    settingsStore.set(updated)
    pickedPreview.value = null // fall back to the freshly-served logo
    toast.success('Logo updated.')
  } catch (error) {
    pickedPreview.value = null
    const message =
      error instanceof ApiError && error.status === 413
        ? 'Logo is too large (max 512 KB).'
        : error instanceof Error
          ? error.message
          : 'Could not upload the logo.'
    toast.error(message)
  } finally {
    uploadingLogo.value = false
  }
}

// -- Storage targets (session 2.2) ------------------------------------------
const targets = ref<StorageTarget[]>([])
const storageLoading = ref(false)
const storageError = ref('')
const testResults = reactive<Record<number, TestConnectionResult>>({})
const testingId = ref<number | null>(null)

const targetSheetOpen = ref(false)
const editingTarget = ref<StorageTarget | null>(null)
const deleteTarget = ref<StorageTarget | null>(null)
const deleting = ref(false)

const providerLabel = (p: string) => STORAGE_PROVIDER_LABEL[p] ?? p

async function loadTargets() {
  if (!canManage) return
  storageLoading.value = true
  storageError.value = ''
  try {
    targets.value = await storageApi.list()
  } catch (error) {
    storageError.value = error instanceof Error ? error.message : 'Could not load storage targets.'
  } finally {
    storageLoading.value = false
  }
}

function openTargetSheet(t: StorageTarget | null) {
  editingTarget.value = t
  targetSheetOpen.value = true
}

async function onTargetSaved() {
  toast.success('Storage target saved.')
  await loadTargets()
}

async function testTarget(t: StorageTarget) {
  if (testingId.value != null) return
  testingId.value = t.id
  try {
    testResults[t.id] = await storageApi.testConnection(t.id)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not run the connection test.')
  } finally {
    testingId.value = null
  }
}

function testTone(r: TestConnectionResult): string {
  if (!r.reachable) return 'text-err'
  return r.writable ? 'text-ok' : 'text-warn'
}

function testLine(r: TestConnectionResult): string {
  if (!r.reachable) return `Not reachable — ${r.error ?? 'connection failed'}.`
  const latency = r.latency_ms != null ? ` · ${r.latency_ms} ms` : ''
  if (!r.writable) return `Reachable, not writable${latency} — ${r.error ?? 'write denied'}.`
  return `Reachable and writable${latency}.`
}

function confirmDelete(t: StorageTarget) {
  deleteTarget.value = t
}

async function doDelete() {
  const t = deleteTarget.value
  if (!t || deleting.value) return
  deleting.value = true
  try {
    await storageApi.remove(t.id)
    delete testResults[t.id]
    deleteTarget.value = null
    toast.success(`Deleted storage target “${t.name}”.`)
    await loadTargets()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not delete the storage target.')
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  load()
  loadTargets()
})
</script>
