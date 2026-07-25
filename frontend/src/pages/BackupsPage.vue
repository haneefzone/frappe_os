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

    <!-- Tab navigation (B4.6) -->
    <nav role="tablist" aria-label="Backup sections" class="flex shrink-0 border-b border-line px-8">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        role="tab"
        type="button"
        :aria-selected="activeTab === tab.key"
        class="fdm-focus -mb-px border-b-2 px-4 py-3 text-label font-medium transition"
        :class="
          activeTab === tab.key
            ? 'border-ink-1 text-ink-1'
            : 'border-transparent text-ink-3 hover:text-ink-2'
        "
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- Backups tab -->
    <div v-if="activeTab === 'backups'" class="min-h-0 flex-1 overflow-y-auto p-8">
      <!-- KPI header -->
      <div class="mb-6 grid gap-4 sm:grid-cols-3">
        <KPICard
          label="Backup compliance"
          :value="compliance.label"
          :status="compliance.status"
          :sublabel="compliance.sublabel"
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
              <th class="px-4 py-2 font-medium">Storage</th>
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
                <StatusBadge
                  v-if="b.moved_from_backup_id"
                  class="ml-1"
                  status="muted"
                  label="Moved"
                  :title="`Moved from backup #${b.moved_from_backup_id} on another server`"
                />
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
                  :status="storageChip(b.storage_state).status"
                  :label="storageChip(b.storage_state).label"
                  :title="storageTitle(b)"
                />
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
                    :title="`Download ${artifactLabel(art)} (local)`"
                    download
                  >
                    {{ shortArtifact(art) }}
                  </a>
                  <button
                    v-for="art in (canDownload && b.storage_state === 'offsite' ? b.offsite_artifacts : [])"
                    :key="`s3-${art}`"
                    type="button"
                    class="fdm-focus inline-flex items-center gap-0.5 rounded px-1.5 py-1 text-meta text-ok transition hover:bg-raised disabled:opacity-50"
                    :title="`Download ${artifactLabel(art)} from offsite storage (S3)`"
                    :disabled="offsiteBusy === `${b.id}:${art}`"
                    @click="downloadOffsite(b, art)"
                  >
                    <LucideCloud class="h-3 w-3" />{{ shortArtifact(art) }}
                  </button>
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
                    v-if="canMove && b.status === 'success' && b.storage_state === 'offsite'"
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    label="Move"
                    title="Move this backup onto another server (via its offsite copy)"
                    :aria-label="`Move ${b.site_name} backup to another server`"
                    @click="openMove(b)"
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

    <!-- Policies tab (B4.6) -->
    <div v-else-if="activeTab === 'policies'" class="min-h-0 flex-1 overflow-y-auto p-8">
      <div class="mb-4 flex items-center justify-between">
        <div>
          <h2 class="text-section font-semibold text-ink-1">Backup policies</h2>
          <p class="text-label text-ink-3">
            Per-site compliance targets — RPO, retention, and offsite requirements.
          </p>
        </div>
        <Button
          v-if="canManagePolicy"
          variant="subtle"
          theme="gray"
          label="Evaluate now"
          :loading="evaluating"
          :disabled="evaluating || loading"
          @click="evaluateNow"
        >
          <template #prefix><LucideRefreshCw class="h-3.5 w-3.5" /></template>
        </Button>
      </div>

      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <div v-if="loading" class="rounded-lg border border-line bg-surface">
        <div v-for="i in 4" :key="i" class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0">
          <div class="h-3.5 w-40 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-20 animate-pulse rounded bg-raised" />
        </div>
      </div>

      <EmptyState
        v-else-if="sites.length === 0"
        :icon="LucideShieldCheck"
        title="No sites"
        message="Discover a bench to inventory its sites, then set a backup policy per site."
      />

      <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
        <table class="w-full text-left">
          <thead>
            <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
              <th class="px-4 py-2 font-medium">Site</th>
              <th class="px-4 py-2 font-medium">Compliance</th>
              <th class="px-4 py-2 font-medium">RPO</th>
              <th class="px-4 py-2 font-medium">Retention</th>
              <th class="px-4 py-2 font-medium">Offsite</th>
              <th class="px-4 py-2 font-medium">Last backup</th>
              <th v-if="canManagePolicy" class="px-4 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line text-label">
            <tr v-for="row in policyRows" :key="row.site.id">
              <td class="px-4 py-2.5">
                <button
                  type="button"
                  class="fdm-focus rounded font-medium text-ink-1 hover:underline"
                  @click="router.push(`/sites/${row.site.id}`)"
                >
                  {{ row.site.name }}
                </button>
                <div class="truncate font-mono text-meta text-ink-3">{{ row.site.bench_name }}</div>
              </td>
              <td class="px-4 py-2.5">
                <span class="flex items-center gap-1.5">
                  <StatusDot :status="complianceDot(row.state)" />
                  <span class="text-ink-2">{{ complianceLabel(row.state) }}</span>
                </span>
                <span
                  v-if="row.breaches.length"
                  class="mt-0.5 block truncate text-meta text-err"
                  :title="row.breaches.map((b) => b.detail).join('\n')"
                >
                  {{ row.breaches[0].detail }}
                </span>
              </td>
              <td class="px-4 py-2.5 tabular-nums text-ink-2">
                {{ row.policy ? `${row.policy.rpo_hours}h` : '—' }}
              </td>
              <td class="px-4 py-2.5 tabular-nums text-ink-2">
                {{ row.policy && row.policy.retention_days != null ? `${row.policy.retention_days}d` : '—' }}
              </td>
              <td class="px-4 py-2.5">
                <StatusBadge
                  v-if="row.policy?.require_offsite"
                  status="ok"
                  label="Required"
                />
                <span v-else class="text-ink-3">—</span>
              </td>
              <td
                class="px-4 py-2.5 text-ink-3"
                :title="row.lastBackupAt ? absoluteTime(row.lastBackupAt) : undefined"
              >
                {{ row.lastBackupAt ? relativeTime(row.lastBackupAt) : '—' }}
              </td>
              <td v-if="canManagePolicy" class="px-4 py-2.5">
                <div class="flex justify-end">
                  <Button
                    variant="subtle"
                    theme="gray"
                    size="sm"
                    :label="row.policy ? 'Edit policy' : 'Set policy'"
                    @click="openPolicy(row.site)"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Policy editor sheet -->
    <PolicySheet
      :open="policyOpen"
      :site="policySite"
      @close="policyOpen = false"
      @saved="onPolicySaved"
    />

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
          <div
            ref="backupPanel"
            role="dialog"
            aria-modal="true"
            aria-label="Backup now"
            tabindex="-1"
            class="fdm-focus w-full max-w-md rounded-lg border border-line bg-raised"
            @keydown.esc="closeBackup"
            @keydown.tab="trapBackupFocus"
          >
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
              <div v-if="storageTargets.length">
                <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="pick-target">
                  Offsite storage
                </label>
                <select id="pick-target" v-model="backupForm.storageTargetId" v-bind="modalInput">
                  <option :value="'auto'">
                    {{ storageTargets.length === 1 ? `Push to ${storageTargets[0].name}` : 'Auto (skip if ambiguous)' }}
                  </option>
                  <option :value="'local'">Local only (no offsite copy)</option>
                  <option v-for="t in storageTargets" :key="t.id" :value="t.id">Push to {{ t.name }}</option>
                </select>
                <p class="mt-1 text-meta text-ink-3">
                  Artifacts upload after the backup, with each checksum re-verified offsite.
                </p>
              </div>
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

    <!-- Move backup modal (session 2.6) -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="moveOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="closeMove"
        >
          <div
            ref="movePanel"
            role="dialog"
            aria-modal="true"
            aria-label="Move backup to another server"
            tabindex="-1"
            class="fdm-focus w-full max-w-md rounded-lg border border-line bg-raised"
            @keydown.esc="closeMove"
            @keydown.tab="trapMoveFocus"
          >
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Move to another server</h2>
              <p class="mt-0.5 text-label text-ink-2">
                Copy <span class="font-medium text-ink-1">{{ moveSource?.site_name }}</span>'s
                backup onto another server. Its artifacts stream from offsite storage, each
                checksum is re-verified on arrival, and the moved copy is registered there.
              </p>
            </div>
            <div class="space-y-4 px-5 py-4">
              <div v-if="moveTargetBenches.length">
                <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="move-bench">
                  Destination bench
                </label>
                <select id="move-bench" v-model="moveForm.benchId" v-bind="modalInput">
                  <option v-for="bn in moveTargetBenches" :key="bn.id" :value="bn.id">
                    {{ serverName(bn.server_id) }} — {{ bn.name }}
                  </option>
                </select>
                <p class="mt-1 text-meta text-ink-3">
                  Only benches on a different server are listed. The destination site
                  (<span class="font-medium text-ink-2">{{ moveSource?.site_name }}</span>) must
                  already exist there.
                </p>
              </div>
              <p v-else class="text-label text-ink-3">
                No eligible destination. Register a second server with a bench that already
                hosts a site named
                <span class="font-medium text-ink-2">{{ moveSource?.site_name }}</span>.
              </p>
            </div>
            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="busy" @click="closeMove" />
              <Button
                variant="solid"
                theme="gray"
                label="Move backup"
                :loading="busy"
                :disabled="!moveTargetBenches.length"
                @click="submitMove"
              />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import LucideArchive from '~icons/lucide/archive'
import LucideCheck from '~icons/lucide/check'
import LucideCloud from '~icons/lucide/cloud'
import LucideHistory from '~icons/lucide/history'
import LucideRefreshCw from '~icons/lucide/refresh-cw'
import LucideShieldCheck from '~icons/lucide/shield-check'
import { backupsApi, type Backup } from '../api/backups'
import { benchesApi, type Bench } from '../api/benches'
import { ApiError } from '../api/client'
import {
  complianceApi,
  type BackupPolicy,
  type ComplianceState,
  type ComplianceStatus,
  type ComplianceSummary,
} from '../api/compliance'
import { serversApi, type Server } from '../api/servers'
import { sitesApi, type Site } from '../api/sites'
import { storageApi, type StorageTarget } from '../api/storage'
import EmptyState from '../components/EmptyState.vue'
import KPICard from '../components/KPICard.vue'
import PolicySheet from '../components/PolicySheet.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import type { Status } from '../components/types'
import { toast } from '../components/toast'
import {
  ARTIFACT_LABEL,
  BACKUP_TYPE_LABEL,
  backupStatusDot as statusDot,
  formatBytes,
  storageChip,
  totalSize,
} from '../lib/backups'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const canBackup = auth.hasPermission('backup:create')
const canRestore = auth.hasPermission('backup:restore')
const canDownload = auth.hasPermission('backup:restore')
const canManagePolicy = auth.hasPermission('schedule:manage')
const canMove = auth.hasPermission('backup:transfer')

const backups = ref<Backup[]>([])
const sites = ref<Site[]>([])
const benches = ref<Bench[]>([])
const servers = ref<Server[]>([])
const storageTargets = ref<StorageTarget[]>([])
const summary = ref<ComplianceSummary | null>(null)
const policies = ref<Record<number, BackupPolicy>>({})
const loading = ref(true)
const loadError = ref('')
const busy = ref(false)
const offsiteBusy = ref('')
const evaluating = ref(false)

// -- Tabs (B4.6) -------------------------------------------------------------
type TabKey = 'backups' | 'policies'
const TABS: { key: TabKey; label: string }[] = [
  { key: 'backups', label: 'Backups' },
  { key: 'policies', label: 'Policies' },
]
const activeTab = ref<TabKey>('backups')

function switchTab(key: TabKey) {
  activeTab.value = key
  if (key === 'policies' && Object.keys(policies.value).length === 0) {
    void loadPolicies()
  }
}

// -- Policy editor sheet -----------------------------------------------------
const policyOpen = ref(false)
const policySite = ref<Site | null>(null)

function openPolicy(site: Site) {
  policySite.value = site
  policyOpen.value = true
}

async function onPolicySaved() {
  await Promise.all([loadPolicies(), loadSummary()])
}

const modalInput = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 focus:border-line-strong',
}

const downloadUrl = backupsApi.downloadUrl
const typeLabel = (t: string) => BACKUP_TYPE_LABEL[t] ?? t
const artifactLabel = (a: string) => ARTIFACT_LABEL[a] ?? a

function storageTitle(b: Backup): string {
  if (b.storage_state === 'offsite') {
    const name = storageTargets.value.find((t) => t.id === b.storage_target_id)?.name
    return name ? `Offsite in ${name}, checksums re-verified` : 'Offsite, checksums re-verified'
  }
  if (b.storage_state === 'uploading') return 'Upload in progress'
  if (b.storage_state === 'failed') return 'Offsite upload failed — artifacts remain local'
  return 'Stored on the source server only'
}
const shortArtifact = (a: string) =>
  ({ database: 'DB', public_files: 'Pub', private_files: 'Priv', config: 'Cfg' })[a] ?? a

const totalSizeLabel = computed(() => formatBytes(totalSize(backups.value)))

// Real fleet compliance from the evaluator (session 2.3), not derived client-side.
const compliance = computed(() => {
  const s = summary.value
  const policied = s?.policied ?? 0
  if (policied === 0) {
    return { label: '—', sublabel: 'No policies', status: 'muted' as Status }
  }
  const pct = s?.compliance_pct ?? 0
  return {
    label: `${pct}%`,
    sublabel: `${s?.compliant ?? 0}/${policied} sites compliant`,
    status: (pct >= 90 ? 'ok' : pct >= 50 ? 'warn' : 'err') as Status,
  }
})

// -- Policies tab rows -------------------------------------------------------
const statusBySite = computed(() => {
  const map = new Map<number, ComplianceStatus>()
  for (const st of summary.value?.statuses ?? []) map.set(st.site_id, st)
  return map
})

const policyRows = computed(() =>
  sites.value.map((site) => {
    const st = statusBySite.value.get(site.id)
    return {
      site,
      policy: policies.value[site.id] ?? null,
      state: st?.state ?? ('unknown' as ComplianceState),
      breaches: st?.breaches ?? [],
      lastBackupAt: st?.last_backup_at ?? null,
    }
  }),
)

function complianceDot(state: ComplianceState): Status {
  if (state === 'compliant') return 'ok'
  if (state === 'breached') return 'err'
  return 'muted'
}

function complianceLabel(state: ComplianceState): string {
  if (state === 'compliant') return 'Compliant'
  if (state === 'breached') return 'Breached'
  return 'No policy'
}

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
    // Storage targets drive the offsite selector/labels; listing needs
    // settings:manage, so tolerate a 403 for non-Admin backup operators.
    void loadStorageTargets()
    // Fleet compliance summary drives the KPI card + Policies tab ticks.
    void loadSummary()
    // Benches + servers drive the cross-server Move picker (Developer+ only).
    if (canMove) void loadMoveTargets()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load backups.'
  } finally {
    loading.value = false
  }
}

async function loadSummary() {
  try {
    summary.value = await complianceApi.getSummary()
  } catch {
    // Non-fatal: the compliance KPI falls back to "—".
    summary.value = null
  }
}

// Per-site policies, loaded lazily when the Policies tab is first opened.
async function loadPolicies() {
  const entries = await Promise.all(
    sites.value.map(async (s) => {
      try {
        return [s.id, await complianceApi.getPolicy(s.id)] as const
      } catch {
        // 404 (no policy) or 403 — leave the site without a policy row.
        return [s.id, null] as const
      }
    }),
  )
  const next: Record<number, BackupPolicy> = {}
  for (const [id, policy] of entries) if (policy) next[id] = policy
  policies.value = next
}

async function evaluateNow() {
  if (evaluating.value) return
  evaluating.value = true
  try {
    summary.value = await complianceApi.evaluate()
    toast.success('Compliance re-evaluated.')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not evaluate compliance.')
  } finally {
    evaluating.value = false
  }
}

async function loadStorageTargets() {
  try {
    storageTargets.value = (await storageApi.list()).filter((t) => t.enabled)
  } catch {
    // 403 for non-Admin operators: no selector, backend auto-selects offsite.
    storageTargets.value = []
  }
}

async function loadMoveTargets() {
  try {
    const [bn, sv] = await Promise.all([benchesApi.list(), serversApi.list()])
    benches.value = bn
    servers.value = sv
  } catch {
    benches.value = []
    servers.value = []
  }
}

const serverName = (id: number) => servers.value.find((s) => s.id === id)?.name ?? `Server #${id}`

// -- Move to another server (session 2.6) ------------------------------------
const moveOpen = ref(false)
const movePanel = ref<HTMLElement | null>(null)
const moveSource = ref<Backup | null>(null)
const moveForm = reactive({ benchId: null as number | null })

// Benches on a DIFFERENT server than the source backup that already host a site
// with the same name (the destination the moved copy registers against).
const moveTargetBenches = computed(() => {
  const src = moveSource.value
  if (!src) return []
  const siteName = src.site_name
  const benchIdsWithSite = new Set(
    sites.value.filter((s) => s.name === siteName).map((s) => s.bench_id),
  )
  return benches.value.filter(
    (bn) => bn.server_id !== src.server_id && benchIdsWithSite.has(bn.id),
  )
})

function openMove(b: Backup) {
  moveSource.value = b
  moveForm.benchId = moveTargetBenches.value[0]?.id ?? null
  moveOpen.value = true
}

function closeMove() {
  if (!busy.value) moveOpen.value = false
}

watch(moveOpen, (open) => {
  if (open) nextTick(() => movePanel.value?.focus())
})

function trapMoveFocus(event: KeyboardEvent) {
  const panel = movePanel.value
  if (!panel) return
  const focusable = Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
  if (focusable.length === 0) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey) {
    if (document.activeElement === first || document.activeElement === panel) {
      event.preventDefault()
      last.focus()
    }
  } else if (document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

async function submitMove() {
  if (busy.value || moveForm.benchId == null || !moveSource.value) return
  busy.value = true
  try {
    const job = await backupsApi.move(moveSource.value.id, { target_bench_id: moveForm.benchId })
    moveOpen.value = false
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on the destination site.'
        : error instanceof Error
          ? error.message
          : 'Could not start the move.'
    toast.error(message)
  } finally {
    busy.value = false
  }
}

// -- Backup now --------------------------------------------------------------
const backupOpen = ref(false)
const backupPanel = ref<HTMLElement | null>(null)
const backupForm = reactive({
  siteId: 'all' as number | 'all',
  withFiles: true,
  // 'auto' = let the API auto-select; 'local' = force local-only; else a target id.
  storageTargetId: 'auto' as number | 'auto' | 'local',
})

function openBackup() {
  backupForm.siteId = sites.value.length ? sites.value[0].id : 'all'
  backupForm.withFiles = true
  backupForm.storageTargetId = 'auto'
  backupOpen.value = true
}

// Move focus into the dialog when it opens so keyboard users start inside it
// and Esc works immediately (mirrors ConfirmModal).
watch(backupOpen, (open) => {
  if (open) nextTick(() => backupPanel.value?.focus())
})

// Keep Tab/Shift-Tab cycling within the dialog while it is open (a11y, F3).
function trapBackupFocus(event: KeyboardEvent) {
  const panel = backupPanel.value
  if (!panel) return
  const focusable = Array.from(
    panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
  if (focusable.length === 0) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey) {
    if (document.activeElement === first || document.activeElement === panel) {
      event.preventDefault()
      last.focus()
    }
  } else if (document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

// Map the modal selection to the API's storage_target_id contract:
// omit (undefined) = auto-select; 0 = force local-only; a positive id = that target.
function storagePayload(): number | undefined {
  if (backupForm.storageTargetId === 'auto') return undefined
  if (backupForm.storageTargetId === 'local') return 0
  return backupForm.storageTargetId
}

function closeBackup() {
  if (!busy.value) backupOpen.value = false
}

async function submitBackup() {
  if (busy.value) return
  busy.value = true
  try {
    const storage_target_id = storagePayload()
    if (backupForm.siteId === 'all') {
      let launched = 0
      for (const s of sites.value) {
        try {
          await backupsApi.create(s.id, { with_files: backupForm.withFiles, storage_target_id })
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
      const job = await backupsApi.create(backupForm.siteId, {
        with_files: backupForm.withFiles,
        storage_target_id,
      })
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

async function downloadOffsite(b: Backup, artifact: string) {
  const token = `${b.id}:${artifact}`
  if (offsiteBusy.value === token) return
  offsiteBusy.value = token
  try {
    // The endpoint returns a short-lived presigned URL (Developer+, audited);
    // opening it downloads straight from S3 — the secret key never travels.
    const { url } = await backupsApi.offsiteDownload(b.id, artifact)
    window.open(url, '_blank', 'noopener')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not sign the offsite download.')
  } finally {
    offsiteBusy.value = ''
  }
}

onMounted(load)
</script>
