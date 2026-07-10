<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Backups</h1>
        <p class="text-meta text-ink-2">
          Backup inventory with per-artifact checksums, sizes, and restore-tested status.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <RouterLink
          to="/restore"
          class="fdm-focus inline-flex items-center gap-2 rounded-lg border border-line px-3 py-1.5 text-label font-medium text-ink-1 transition hover:border-line-strong hover:bg-raised"
        >
          <LucideHistory class="h-4 w-4" />
          Restore
        </RouterLink>
        <Button v-if="canBackup" variant="solid" theme="gray" label="Backup now" @click="openBackup">
          <template #prefix><LucideArchive class="h-4 w-4" /></template>
        </Button>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <!-- KPI header -->
      <div class="mb-6 grid gap-4 sm:grid-cols-3">
        <KPICard
          label="Backup compliance"
          :value="compliance.label"
          :status="compliance.status"
          :sublabel="`${compliance.covered}/${compliance.total} sites backed up`"
          :loading="loading"
        />
        <KPICard label="Total size" :value="totalSizeLabel" :sublabel="`${backups.length} backups`" :loading="loading" />
        <KPICard
          label="Last failure"
          :value="lastFailureLabel"
          :status="lastFailure ? 'err' : 'ok'"
          :sublabel="lastFailure ? lastFailure.site_name : 'No recent failures'"
          :loading="loading"
        />
      </div>

      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <div v-if="loading" class="rounded-lg border border-line bg-surface">
        <div v-for="i in 4" :key="i" class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0">
          <div class="h-3.5 w-40 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
        </div>
      </div>

      <EmptyState
        v-else-if="backups.length === 0"
        :icon="LucideArchive"
        title="No backups yet"
        :message="canBackup ? 'Use Backup now to capture your first backup.' : 'No backups have been captured yet.'"
        :cta-label="canBackup ? 'Backup now' : undefined"
        @cta="openBackup"
      />

      <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
        <table class="w-full text-left">
          <thead>
            <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
              <th class="px-4 py-2 font-medium">Site</th>
              <th class="px-4 py-2 font-medium">Type</th>
              <th class="px-4 py-2 font-medium">Size</th>
              <th class="px-4 py-2 font-medium">Integrity</th>
              <th class="px-4 py-2 font-medium">Restore-tested</th>
              <th class="px-4 py-2 font-medium">Taken</th>
              <th class="px-4 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line text-label">
            <tr v-for="b in backups" :key="b.id">
              <td class="px-4 py-2.5">
                <div class="flex items-center gap-2">
                  <StatusDot :status="statusDot(b.status)" />
                  <span class="font-medium text-ink-1">{{ b.site_name }}</span>
                </div>
                <span class="text-meta text-ink-3">{{ b.bench_name }}</span>
              </td>
              <td class="px-4 py-2.5">
                <StatusBadge :status="b.type === 'with-files' ? 'ok' : 'muted'" :label="typeLabel(b.type)" />
              </td>
              <td class="px-4 py-2.5 tabular-nums text-ink-2">{{ formatBytes(b.size_bytes) }}</td>
              <td class="px-4 py-2.5">
                <span
                  v-if="b.status === 'success' && b.artifacts.length"
                  class="inline-flex items-center gap-1 text-ok"
                  :title="`${b.artifacts.length} artifact(s), sha256 recorded`"
                >
                  <LucideCheck class="h-3.5 w-3.5" /> {{ b.artifacts.length }}
                </span>
                <span v-else class="text-ink-3">—</span>
              </td>
              <td class="px-4 py-2.5">
                <StatusBadge
                  v-if="b.restore_tested"
                  status="ok"
                  label="Tested"
                />
                <span v-else class="text-ink-3">Not tested</span>
              </td>
              <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(b.created_at)">{{ relativeTime(b.created_at) }}</td>
              <td class="px-4 py-2.5">
                <div class="flex items-center justify-end gap-1">
                  <a
                    v-for="art in b.available_artifacts"
                    v-show="canDownload"
                    :key="art"
                    :href="downloadUrl(b.id, art)"
                    class="fdm-focus rounded px-1.5 py-1 text-meta text-ink-3 transition hover:bg-raised hover:text-ink-1"
                    :title="`Download ${artifactLabel(art)}`"
                    download
                  >
                    {{ shortArtifact(art) }}
                  </a>
                  <Button
                    v-if="canBackup"
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    label="Verify"
                    :disabled="busy"
                    @click="validate(b)"
                  />
                  <Button
                    v-if="canRestore && b.status === 'success'"
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    label="Restore →"
                    @click="router.push(`/restore?backup=${b.id}`)"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Backup now modal -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="backupOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="closeBackup"
        >
          <div role="dialog" aria-modal="true" aria-label="Backup now" class="w-full max-w-md rounded-lg border border-line bg-raised">
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Backup now</h2>
              <p class="mt-0.5 text-label text-ink-2">Capture a backup of one site or every site.</p>
            </div>
            <div class="space-y-4 px-5 py-4">
              <div>
                <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="pick-site">Site</label>
                <select id="pick-site" v-model="backupForm.siteId" v-bind="modalInput">
                  <option :value="'all'">All sites ({{ sites.length }})</option>
                  <option v-for="s in sites" :key="s.id" :value="s.id">{{ s.name }} — {{ s.bench_name }}</option>
                </select>
              </div>
              <label class="flex cursor-pointer items-center gap-2 text-label text-ink-1">
                <input v-model="backupForm.withFiles" type="checkbox" class="accent-white" />
                Include files (public + private) — larger, slower
              </label>
              <p class="text-meta text-ink-3">
                Each backup runs as a job; you'll land on its live log (or the Jobs page for all sites).
              </p>
            </div>
            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="busy" @click="closeBackup" />
              <Button variant="solid" theme="gray" label="Start backup" :loading="busy" @click="submitBackup" />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import LucideArchive from '~icons/lucide/archive'
import LucideCheck from '~icons/lucide/check'
import LucideHistory from '~icons/lucide/history'
import { backupsApi, type Backup } from '../api/backups'
import { ApiError } from '../api/client'
import { sitesApi, type Site } from '../api/sites'
import EmptyState from '../components/EmptyState.vue'
import KPICard from '../components/KPICard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import {
  ARTIFACT_LABEL,
  BACKUP_TYPE_LABEL,
  backupStatusDot as statusDot,
  formatBytes,
  totalSize,
} from '../lib/backups'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const canBackup = auth.hasPermission('backup:create')
const canRestore = auth.hasPermission('backup:restore')
const canDownload = auth.hasPermission('backup:restore')

const backups = ref<Backup[]>([])
const sites = ref<Site[]>([])
const loading = ref(true)
const loadError = ref('')
const busy = ref(false)

const modalInput = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 focus:border-line-strong',
}

const downloadUrl = backupsApi.downloadUrl
const typeLabel = (t: string) => BACKUP_TYPE_LABEL[t] ?? t
const artifactLabel = (a: string) => ARTIFACT_LABEL[a] ?? a
const shortArtifact = (a: string) =>
  ({ database: 'DB', public_files: 'Pub', private_files: 'Priv', config: 'Cfg' })[a] ?? a

const totalSizeLabel = computed(() => formatBytes(totalSize(backups.value)))

const compliance = computed(() => {
  const total = sites.value.length
  const backedUp = new Set(
    backups.value.filter((b) => b.status === 'success').map((b) => b.site_id),
  )
  const covered = [...backedUp].filter((id) => sites.value.some((s) => s.id === id)).length
  const pct = total ? Math.round((covered / total) * 100) : 0
  return {
    label: total ? `${pct}%` : '—',
    covered,
    total,
    status: (pct >= 100 ? 'ok' : pct >= 50 ? 'warn' : 'err') as 'ok' | 'warn' | 'err',
  }
})

const lastFailure = computed(() => backups.value.find((b) => b.status === 'failed') ?? null)
const lastFailureLabel = computed(() =>
  lastFailure.value ? relativeTime(lastFailure.value.created_at) : 'None',
)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [bk, st] = await Promise.all([backupsApi.list(), sitesApi.list()])
    // Newest first (the API already sorts, but keep it explicit for safety).
    backups.value = bk
    sites.value = st
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load backups.'
  } finally {
    loading.value = false
  }
}

// -- Backup now --------------------------------------------------------------
const backupOpen = ref(false)
const backupForm = reactive({ siteId: 'all' as number | 'all', withFiles: true })

function openBackup() {
  backupForm.siteId = sites.value.length ? sites.value[0].id : 'all'
  backupForm.withFiles = true
  backupOpen.value = true
}

function closeBackup() {
  if (!busy.value) backupOpen.value = false
}

async function submitBackup() {
  if (busy.value) return
  busy.value = true
  try {
    if (backupForm.siteId === 'all') {
      let launched = 0
      for (const s of sites.value) {
        try {
          await backupsApi.create(s.id, { with_files: backupForm.withFiles })
          launched++
        } catch {
          // keep going; one busy site shouldn't block the rest
        }
      }
      backupOpen.value = false
      toast.success(`Started ${launched} backup job(s).`)
      await load()
      router.push('/jobs')
    } else {
      const job = await backupsApi.create(backupForm.siteId, { with_files: backupForm.withFiles })
      backupOpen.value = false
      router.push(`/jobs/${job.id}`)
    }
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on that site.'
        : error instanceof Error
          ? error.message
          : 'Could not start the backup.'
    toast.error(message)
  } finally {
    busy.value = false
  }
}

async function validate(b: Backup) {
  if (busy.value) return
  busy.value = true
  try {
    const job = await backupsApi.validate(b.id)
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not start verification.')
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>
