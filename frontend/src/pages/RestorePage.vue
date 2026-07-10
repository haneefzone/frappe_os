<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Restore</h1>
        <p class="text-meta text-ink-2">
          Pick a backup, choose a target, confirm — then watch the restore run.
        </p>
      </div>
      <Button variant="subtle" theme="gray" label="Back to backups" @click="router.push('/backups')" />
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <div class="mx-auto max-w-2xl">
        <Wizard
          v-model="active"
          :steps="steps"
          submit-label="Restore now"
          :can-continue="canContinue"
          :submitting="submitting"
          @submit="submit"
        >
          <!-- 1. Pick backup -->
          <template #step-backup>
            <EmptyState
              v-if="!successful.length"
              :icon="LucideArchive"
              title="No restorable backups"
              message="Capture a successful backup first, then come back to restore it."
              cta-label="Go to backups"
              @cta="router.push('/backups')"
            />
            <div v-else class="space-y-2">
              <label
                v-for="b in successful"
                :key="b.id"
                class="fdm-focus-within flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-3 transition"
                :class="form.backupId === b.id ? 'border-line-strong bg-raised' : 'border-line hover:border-line-strong'"
              >
                <input v-model.number="form.backupId" type="radio" name="backup" :value="b.id" class="accent-white" @change="onPickBackup" />
                <div class="min-w-0">
                  <div class="flex items-center gap-2">
                    <span class="font-medium text-ink-1">{{ b.site_name }}</span>
                    <StatusBadge :status="b.type === 'with-files' ? 'ok' : 'muted'" :label="typeLabel(b.type)" />
                    <span v-if="b.frappe_version" class="text-meta text-ink-3">v{{ b.frappe_version }}</span>
                  </div>
                  <span class="text-meta text-ink-3">{{ b.bench_name }} · {{ formatBytes(b.size_bytes) }} · {{ relativeTime(b.created_at) }}</span>
                </div>
              </label>

              <!-- Inline validation job -->
              <div v-if="form.backupId" class="mt-3 rounded-lg border border-line bg-surface px-4 py-3">
                <div class="flex items-center gap-2 text-label">
                  <StatusDot :status="validation.dot" />
                  <span class="font-medium text-ink-1">Integrity check</span>
                  <span class="text-ink-3">{{ validation.message }}</span>
                </div>
                <ul v-if="validation.result" class="mt-2 space-y-0.5 text-meta">
                  <li v-for="a in validation.result.artifacts" :key="a.kind" class="flex items-center gap-1.5">
                    <LucideCheck v-if="a.ok" class="h-3 w-3 text-ok" />
                    <LucideX v-else class="h-3 w-3 text-err" />
                    <span :class="a.ok ? 'text-ink-2' : 'text-err'">{{ artifactLabel(a.kind) }}</span>
                  </li>
                </ul>
              </div>
            </div>
          </template>

          <!-- 2. Target -->
          <template #step-target>
            <div class="space-y-4">
              <div class="grid grid-cols-1 gap-2" role="radiogroup" aria-label="Restore target">
                <button
                  v-for="opt in modeOptions"
                  :key="opt.value"
                  type="button"
                  role="radio"
                  :aria-checked="form.mode === opt.value"
                  class="fdm-focus rounded-lg border px-3 py-2.5 text-left text-label transition"
                  :class="form.mode === opt.value ? 'border-line-strong bg-raised text-ink-1' : 'border-line text-ink-2 hover:border-line-strong'"
                  @click="setMode(opt.value)"
                >
                  <span class="block font-medium text-ink-1">{{ opt.label }}</span>
                  <span class="block text-meta text-ink-3">{{ opt.hint }}</span>
                </button>
              </div>

              <!-- same_site: destructive banner -->
              <p v-if="form.mode === 'same_site'" class="rounded-lg border border-err/40 bg-err/10 px-4 py-3 text-label text-err" role="alert">
                This overwrites <strong>{{ selectedBackup?.site_name }}</strong> in place. Its current
                data is replaced by the backup. An automatic pre-restore backup is taken first.
              </p>

              <!-- new_site / different_bench: bench + site name -->
              <template v-if="form.mode !== 'same_site'">
                <div>
                  <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="target-bench">Target bench</label>
                  <select id="target-bench" v-model.number="form.targetBenchId" v-bind="modalInput" @change="checkCompat">
                    <option :value="null" disabled>Choose a bench…</option>
                    <option v-for="bn in sameServerBenches" :key="bn.id" :value="bn.id">{{ bn.name }} — {{ bn.path }}</option>
                  </select>
                  <p class="mt-1 text-meta text-ink-3">Only benches on the backup's server are shown (cross-server restore isn't supported yet).</p>
                </div>
                <div>
                  <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="target-site">Target site name</label>
                  <input id="target-site" v-model.trim="form.targetSiteName" v-bind="modalInput" placeholder="test2.localhost" />
                  <p class="mt-1 text-meta" :class="targetNameValid ? 'text-ink-3' : 'text-err'">
                    {{ form.mode === 'new_site' ? 'A NEW site created and restored into.' : 'An EXISTING site on that bench (its data is overwritten).' }}
                  </p>
                </div>
                <!-- new_site admin password -->
                <div v-if="form.mode === 'new_site'">
                  <div class="mb-1 flex items-center justify-between">
                    <label class="block text-meta font-medium uppercase tracking-wide text-ink-2" for="admin-pw">Administrator password</label>
                    <Button variant="subtle" theme="gray" size="sm" label="Generate" @click="generatePassword" />
                  </div>
                  <input id="admin-pw" v-model="form.adminPassword" type="text" v-bind="modalInput" class="fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 font-mono text-label text-ink-1" />
                </div>
                <!-- compatibility -->
                <p
                  v-if="compat"
                  class="rounded-lg border px-4 py-3 text-label"
                  :class="compat.ok ? 'border-ok/40 bg-ok/10 text-ok' : 'border-err/40 bg-err/10 text-err'"
                  role="status"
                >
                  {{ compat.reason }}
                </p>
              </template>
            </div>
          </template>

          <!-- 3. Review (red panel) -->
          <template #step-review>
            <div class="space-y-4">
              <div class="rounded-lg border border-err/50 bg-err/10 px-4 py-4">
                <div class="flex items-center gap-2 text-err">
                  <LucideTriangleAlert class="h-4 w-4" />
                  <h3 class="text-section font-semibold">This restore will:</h3>
                </div>
                <ul class="mt-2 space-y-1 text-label text-ink-1">
                  <li v-for="(c, i) in consequences" :key="i" class="flex gap-2">
                    <span class="text-err">•</span><span>{{ c }}</span>
                  </li>
                </ul>
              </div>

              <p v-if="destructive" class="rounded-lg border border-line bg-surface px-4 py-3 text-label text-ink-2">
                <LucideShieldCheck class="mr-1 inline h-4 w-4 text-ok" />
                An automatic <strong>pre-restore backup</strong> (with files) is taken first and shown as a
                step in the job timeline — you can roll back to it.
              </p>

              <!-- type the site name to confirm (destructive only) -->
              <div v-if="destructive">
                <label class="mb-1 block text-label text-ink-2" for="confirm-name">
                  Type <span class="select-all font-mono font-semibold text-ink-1">{{ targetSite }}</span> to confirm
                </label>
                <input id="confirm-name" v-model.trim="form.confirmName" v-bind="modalInput" :placeholder="targetSite" autocomplete="off" />
              </div>
            </div>
          </template>
        </Wizard>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArchive from '~icons/lucide/archive'
import LucideCheck from '~icons/lucide/check'
import LucideShieldCheck from '~icons/lucide/shield-check'
import LucideTriangleAlert from '~icons/lucide/triangle-alert'
import LucideX from '~icons/lucide/x'
import {
  backupsApi,
  parseValidateLine,
  type Backup,
  type Compatibility,
  type RestoreMode,
  type ValidateResult,
} from '../api/backups'
import { benchesApi, type Bench } from '../api/benches'
import { ApiError } from '../api/client'
import { streamJobLogs } from '../api/jobs'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import Wizard from '../components/Wizard.vue'
import { toast } from '../components/toast'
import type { Status, WizardStep } from '../components/types'
import { ARTIFACT_LABEL, BACKUP_TYPE_LABEL, formatBytes } from '../lib/backups'
import { relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const canDanger = auth.hasPermission('danger')

// Mirrors the backend SITE_NAME whitelist.
const NAME_RE = /^[a-z0-9][a-z0-9.-]{1,80}$/

const steps: WizardStep[] = [
  { key: 'backup', label: 'Backup', description: 'Pick a backup and let it validate.' },
  { key: 'target', label: 'Target', description: 'Where should it be restored?' },
  { key: 'review', label: 'Review', description: 'Confirm the consequences, then restore.' },
]

const active = ref(0)
const backups = ref<Backup[]>([])
const benches = ref<Bench[]>([])
const submitting = ref(false)

const modalInput = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 focus:border-line-strong',
}

const form = reactive({
  backupId: null as number | null,
  mode: 'same_site' as RestoreMode,
  targetBenchId: null as number | null,
  targetSiteName: '',
  adminPassword: '',
  confirmName: '',
})

const typeLabel = (t: string) => BACKUP_TYPE_LABEL[t] ?? t
const artifactLabel = (a: string) => ARTIFACT_LABEL[a] ?? a

const successful = computed(() => backups.value.filter((b) => b.status === 'success'))
const selectedBackup = computed(() => backups.value.find((b) => b.id === form.backupId) ?? null)

const modeOptions: { value: RestoreMode; label: string; hint: string }[] = [
  { value: 'same_site', label: 'Same site (overwrite)', hint: 'Restore over the backup’s own site — destructive' },
  { value: 'new_site', label: 'New site', hint: 'Create a fresh site and restore into it' },
  { value: 'different_bench', label: 'Different bench', hint: 'Restore over an existing site on another bench' },
]

const sameServerBenches = computed(() =>
  selectedBackup.value ? benches.value.filter((b) => b.server_id === selectedBackup.value!.server_id) : [],
)

const targetSite = computed(() =>
  form.mode === 'same_site' ? selectedBackup.value?.site_name ?? '' : form.targetSiteName,
)
const targetNameValid = computed(() => form.mode === 'same_site' || NAME_RE.test(form.targetSiteName))
const destructive = computed(() => form.mode === 'same_site' || form.mode === 'different_bench')

const consequences = computed(() => {
  const b = selectedBackup.value
  const site = targetSite.value || '(target)'
  const out: string[] = []
  if (form.mode === 'new_site') {
    out.push(`Create a new site ${site} on the chosen bench.`)
    out.push(`Restore the database${b?.type === 'with-files' ? ' and files' : ''} from the backup into it.`)
  } else {
    out.push(`Overwrite ALL current data in ${site} with the backup.`)
    out.push('Take an automatic pre-restore backup first (recoverable).')
  }
  out.push('Copy the source encryption_key so encrypted fields decrypt (gotcha #7).')
  out.push('Run bench migrate on the restored site.')
  return out
})

// -- inline validation -------------------------------------------------------
const validation = reactive({
  dot: 'muted' as Status,
  message: 'Select a backup to validate.',
  result: null as ValidateResult | null,
})

async function onPickBackup() {
  compat.value = null
  validation.result = null
  if (!form.backupId) return
  validation.dot = 'running'
  validation.message = 'Verifying checksums…'
  try {
    const job = await backupsApi.validate(form.backupId)
    let found: ValidateResult | null = null
    await streamJobLogs(job.id, 0, {
      onLog: (frame) => {
        const parsed = parseValidateLine(frame.content)
        if (parsed) found = parsed
      },
    })
    validation.result = found
    applyValidation()
  } catch (error) {
    validation.dot = 'warn'
    validation.message = error instanceof Error ? error.message : 'Validation could not run.'
  }
}

function applyValidation() {
  const r = validation.result
  if (!r) {
    validation.dot = 'warn'
    validation.message = 'Validation finished but returned no result — proceed with caution.'
    return
  }
  validation.dot = r.all_ok ? 'ok' : 'err'
  validation.message = r.all_ok
    ? `All ${r.artifact_count} artifact(s) verified.`
    : 'Some artifacts failed verification — restoring is not recommended.'
}

// -- compatibility -----------------------------------------------------------
const compat = ref<Compatibility | null>(null)

async function checkCompat() {
  compat.value = null
  if (form.mode === 'same_site' || !form.backupId || !form.targetBenchId) return
  try {
    compat.value = await backupsApi.compatibility(form.backupId, form.targetBenchId)
  } catch {
    compat.value = null
  }
}

function setMode(mode: RestoreMode) {
  form.mode = mode
  form.confirmName = ''
  compat.value = null
  if (mode !== 'same_site' && form.targetBenchId) checkCompat()
}

function generatePassword() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789!@#$%^&*'
  const bytes = new Uint32Array(20)
  crypto.getRandomValues(bytes)
  form.adminPassword = Array.from(bytes, (n) => alphabet[n % alphabet.length]).join('')
}

const canContinue = computed(() => {
  switch (active.value) {
    case 0:
      return form.backupId != null && validation.dot !== 'running'
    case 1:
      if (form.mode === 'same_site') return true
      if (!form.targetBenchId || !targetNameValid.value) return false
      if (form.mode === 'new_site' && form.adminPassword.length === 0) return false
      return compat.value == null || compat.value.ok
    default:
      return destructive.value ? form.confirmName === targetSite.value : true
  }
})

async function load() {
  try {
    const [bk, bn] = await Promise.all([backupsApi.list(), benchesApi.list()])
    backups.value = bk
    benches.value = bn
    const pre = Number(route.query.backup)
    if (pre && bk.some((b) => b.id === pre && b.status === 'success')) {
      form.backupId = pre
      onPickBackup()
    }
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not load backups.')
  }
}

async function submit() {
  if (submitting.value || form.backupId == null) return
  if (destructive.value && !canDanger) {
    toast.error('Your role cannot restore over an existing site (needs the danger permission).')
    return
  }
  submitting.value = true
  try {
    const job = await backupsApi.restore({
      mode: form.mode,
      backup_id: form.backupId,
      target_bench_id: form.mode === 'same_site' ? undefined : form.targetBenchId ?? undefined,
      target_site_name: form.mode === 'same_site' ? undefined : form.targetSiteName,
      admin_password: form.mode === 'new_site' ? form.adminPassword : undefined,
      confirm_name: destructive.value ? form.confirmName : undefined,
    })
    toast.success('Restore started.')
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on that site.'
        : error instanceof ApiError && (error.status === 422 || error.status === 403)
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Could not start the restore.'
    toast.error(message)
    submitting.value = false
  }
}

onMounted(load)
</script>
