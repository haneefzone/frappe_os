<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between gap-4 border-b border-line px-8 py-5">
      <div class="flex min-w-0 items-center gap-3">
        <button
          type="button"
          class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
          aria-label="Back to servers"
          @click="router.push('/servers')"
        >
          <LucideArrowLeft class="h-4 w-4" />
        </button>
        <StatusDot v-if="server" :status="statusDot(server.status)" />
        <h1 class="truncate text-lg font-semibold text-ink-1">{{ server?.name ?? 'Server' }}</h1>
        <EnvironmentBadge v-if="server" :env="server.env_tag" />
      </div>
      <div v-if="server && canManage" class="flex shrink-0 items-center gap-2">
        <Button
          variant="subtle"
          theme="gray"
          :label="launching ? 'Starting…' : 'Run demo job'"
          :loading="launching"
          @click="runDemoJob"
        >
          <template #prefix><LucidePlay class="h-4 w-4" /></template>
        </Button>
        <Button
          variant="subtle"
          theme="gray"
          :label="testing ? 'Testing…' : 'Re-test connection'"
          :loading="testing"
          @click="runTest"
        >
          <template #prefix><LucideRefreshCw class="h-4 w-4" /></template>
        </Button>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>
      <div v-else-if="loading" class="grid max-w-4xl gap-6 lg:grid-cols-2">
        <div class="rounded-lg border border-line bg-surface">
          <div class="border-b border-line px-4 py-2.5"><div class="h-3.5 w-24 animate-pulse rounded bg-raised" /></div>
          <div class="divide-y divide-line">
            <div v-for="i in 8" :key="i" class="flex items-center justify-between px-4 py-2">
              <div class="h-3 w-24 animate-pulse rounded bg-raised" />
              <div class="h-3 w-32 animate-pulse rounded bg-raised" />
            </div>
          </div>
        </div>
        <div class="rounded-lg border border-line bg-surface">
          <div class="border-b border-line px-4 py-2.5"><div class="h-3.5 w-28 animate-pulse rounded bg-raised" /></div>
          <div class="divide-y divide-line">
            <div v-for="j in 8" :key="j" class="flex items-center justify-between px-4 py-2">
              <div class="h-3 w-20 animate-pulse rounded bg-raised" />
              <div class="h-3 w-16 animate-pulse rounded bg-raised" />
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="server">
        <!-- Tab bar: Overview | Packages & Tools -->
        <div role="tablist" class="mb-6 flex gap-1 border-b border-line">
          <button
            v-for="t in serverTabs"
            :key="t.key"
            role="tab"
            type="button"
            :aria-selected="serverTab === t.key"
            class="fdm-focus -mb-px rounded-t px-3 py-2 text-label transition"
            :class="
              serverTab === t.key
                ? 'border-b-2 border-ink-1 font-medium text-ink-1'
                : 'text-ink-3 hover:text-ink-2'
            "
            @click="serverTab = t.key"
          >
            {{ t.label }}
          </button>
        </div>

        <div v-show="serverTab === 'overview'" class="grid max-w-4xl gap-6 lg:grid-cols-2">
        <!-- Per-server rollup (session 2.6, B4.2): KPIs for this server only -->
        <section v-if="rollupCards.length" aria-label="Server summary" class="grid gap-4 sm:grid-cols-2 lg:col-span-2 lg:grid-cols-4">
          <KPICard
            v-for="card in rollupCards"
            :key="card.label"
            :label="card.label"
            :value="card.value"
            :status="card.status"
            :sublabel="card.sublabel"
          />
        </section>

        <!-- Specs -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Overview</h2>
          <dl class="divide-y divide-line">
            <div v-for="spec in specs" :key="spec.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ spec.label }}</dt>
              <dd class="max-w-[60%] truncate text-label text-ink-1" :title="spec.value">{{ spec.value }}</dd>
            </div>
          </dl>
        </section>

        <!-- Detected tools -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Detected tools</h2>
          <table class="w-full text-left">
            <tbody class="divide-y divide-line" aria-live="polite" :aria-busy="testing">
              <tr v-for="tool in toolRows" :key="tool.key" class="text-label">
                <td class="px-4 py-2">
                  <span class="flex items-center gap-2">
                    <LucideLoader2 v-if="tool.status === 'running'" class="h-3.5 w-3.5 animate-spin text-run" />
                    <StatusDot v-else :status="dotOf(tool.status)" />
                    <span class="font-mono text-ink-1">{{ tool.label }}</span>
                  </span>
                </td>
                <td class="px-4 py-2 text-right text-ink-2">
                  {{ tool.value ?? (tool.status === 'fail' ? 'not detected' : '—') }}
                </td>
              </tr>
            </tbody>
          </table>
          <p class="px-4 py-2 text-meta text-ink-3">
            {{ hasRun ? 'Live from the last connection test.' : 'Run a connection test to detect installed tools.' }}
          </p>
        </section>

        <!-- Database settings: the MariaDB root password used by bench new-site -->
        <section v-if="canManage" class="rounded-lg border border-line bg-surface lg:col-span-2">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Database</h2>
          <div class="space-y-3 p-4">
            <div class="flex items-center gap-2 text-label">
              <StatusDot :status="server.has_mariadb_root_password ? 'ok' : 'muted'" />
              <span class="text-ink-1">MariaDB root password</span>
              <span class="text-meta text-ink-3">{{ server.has_mariadb_root_password ? 'set' : 'not set' }}</span>
            </div>
            <p class="text-meta text-ink-3">
              Used server-side by <code class="font-mono">bench new-site</code> (gotcha #4). Stored
              encrypted with Fernet; never sent to the browser or shown in logs.
            </p>
            <div class="flex items-center gap-2">
              <input
                v-model="mariadbPassword"
                type="password"
                :placeholder="server.has_mariadb_root_password ? 'Enter a new password to replace it' : 'Set the MariaDB root password'"
                class="fdm-focus w-full max-w-md rounded-lg border border-line bg-base px-3 py-2 font-mono text-label text-ink-1 placeholder:text-ink-3"
              />
              <Button
                variant="solid"
                theme="gray"
                :label="savingDbPw ? 'Saving…' : 'Save'"
                :loading="savingDbPw"
                :disabled="!mariadbPassword"
                @click="saveMariadbPassword"
              />
            </div>
          </div>
        </section>

        <!-- Live monitoring: resource gauges + service health -->
        <section class="rounded-lg border border-line bg-surface lg:col-span-2">
          <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 class="text-label font-semibold text-ink-1">Live monitoring</h2>
            <span v-if="latest" class="text-meta text-ink-3" :title="absoluteTime(latest.ts)">
              {{ latest.ok ? relativeTime(latest.ts) : 'Last collection failed' }}
            </span>
          </div>

          <div class="grid gap-6 p-4 lg:grid-cols-2">
            <!-- Resource gauges -->
            <div class="space-y-3">
              <div v-if="monLoading && !latest" class="space-y-3">
                <div v-for="i in 3" :key="i" class="h-6 animate-pulse rounded bg-raised" />
              </div>
              <template v-else-if="latest">
                <ResourceGauge label="CPU" :pct="latest.cpu_pct" />
                <ResourceGauge label="RAM" :pct="latest.mem_pct" />
                <ResourceGauge label="Disk" :pct="latest.disk_pct" />
                <div class="flex items-center justify-between pt-1 text-label">
                  <span class="text-meta uppercase tracking-wide text-ink-3">Load (1m)</span>
                  <span class="tabular-nums text-ink-1">{{ latest.load1 != null ? latest.load1.toFixed(2) : '—' }}</span>
                </div>
                <p v-if="latest.error" class="text-meta text-err">{{ latest.error }}</p>
              </template>
              <p v-else class="text-label text-ink-3">No samples collected yet.</p>
            </div>

            <!-- Services grid -->
            <div class="space-y-2" aria-live="polite" aria-label="Service status">
              <div v-for="svc in SERVICES" :key="svc.key" class="flex items-center gap-3">
                <StatusDot :status="serviceDot(serviceState(svc.key))" />
                <span class="font-mono text-label text-ink-1">{{ svc.label }}</span>
                <span class="text-meta text-ink-2">{{ serviceState(svc.key) }}</span>
                <Button
                  v-if="canManage"
                  class="ml-auto"
                  variant="subtle"
                  theme="gray"
                  size="sm"
                  :label="`Restart ${svc.label}`"
                  :disabled="restarting !== null"
                  :loading="restarting === svc.key"
                  @click="askRestart(svc.key)"
                />
              </div>
              <p v-if="monError" class="text-meta text-err" role="alert">{{ monError }}</p>
            </div>
          </div>
        </section>

        <!-- Connection checks (appear while/after testing) -->
        <section v-if="hasRun" class="rounded-lg border border-line bg-surface lg:col-span-2">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Connection checks</h2>
          <ul class="grid gap-x-8 gap-y-1 px-4 py-3 sm:grid-cols-2" aria-live="polite" :aria-busy="testing">
            <li v-for="c in coreRows" :key="c.key" class="flex items-center gap-2 text-label">
              <LucideLoader2 v-if="c.status === 'running'" class="h-3.5 w-3.5 animate-spin text-run" />
              <StatusDot v-else :status="dotOf(c.status)" />
              <span class="text-ink-1">{{ c.label }}</span>
              <span class="ml-auto truncate text-meta text-ink-2" :title="c.value ?? ''">{{ c.value ?? '' }}</span>
            </li>
          </ul>
          <p v-if="testError" class="px-4 pb-3 text-label text-err" role="alert">{{ testError }}</p>
        </section>

        <!-- Config drift (session 6.7) -->
        <section class="rounded-lg border border-line bg-surface lg:col-span-2">
          <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 class="text-label font-semibold text-ink-1">Config drift</h2>
            <div class="flex items-center gap-2">
              <span v-if="driftBaselines.length" class="text-meta text-ink-3">
                {{ driftedCount }} drifted of {{ driftBaselines.length }}
              </span>
              <Button
                v-if="canManage"
                variant="subtle"
                theme="gray"
                size="sm"
                :label="runningDriftCheck ? 'Starting…' : 'Run check now'"
                :loading="runningDriftCheck"
                @click="triggerDriftCheck"
              />
            </div>
          </div>
          <div v-if="driftLoading" class="flex flex-wrap gap-2 p-4">
            <div v-for="i in 4" :key="i" class="h-6 w-28 animate-pulse rounded-full bg-raised" />
          </div>
          <div v-else-if="driftBaselines.length === 0" class="px-4 py-3 text-label text-ink-3">
            No config baselines tracked yet. Baselines are captured by managed jobs (install, configure).
          </div>
          <div v-else class="flex flex-wrap gap-2 p-4">
            <DriftChip
              v-for="b in driftBaselines"
              :key="b.id"
              :baseline="b"
              @click="activeDriftId = b.id"
            />
          </div>
        </section>
        </div>

        <!-- Packages & Tools tab -->
        <div v-show="serverTab === 'packages'" class="max-w-4xl">
          <ToolsChecklist :server-id="serverId" />
        </div>
      </div>
    </div>

  <DriftDrawer
    :baseline-id="activeDriftId"
    @close="activeDriftId = null"
    @accepted="loadDrift"
  />

    <!-- Restart confirmation: a service restart is disruptive (spec B5). -->
    <ConfirmModal
      v-model="restartModalOpen"
      variant="destructive"
      :title="pendingService ? `Restart ${pendingService}` : 'Restart service'"
      :message="pendingService ? `This restarts ${pendingService} on ${server?.name ?? 'this server'}.` : ''"
      :verb="pendingService ? `Restart ${pendingService}` : 'Restart'"
      :consequences="pendingService ? [
        `${pendingService} will briefly stop and start again.`,
        'In-flight requests to this service may be dropped.',
      ] : []"
      :loading="restarting !== null"
      @confirm="confirmRestart"
      @cancel="pendingService = null"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import LucideLoader2 from '~icons/lucide/loader-2'
import LucidePlay from '~icons/lucide/play'
import LucideRefreshCw from '~icons/lucide/refresh-cw'
import { jobsApi } from '../api/jobs'
import {
  SERVICES,
  monitoringApi,
  type MonitoringSample,
  type ServiceName,
  type ServiceState,
} from '../api/monitoring'
import {
  serversApi,
  streamServerTest,
  type CheckEvent,
  type Server,
  type ServerDashboard,
} from '../api/servers'
import { driftApi, type DriftBaseline } from '../api/drift'
import ConfirmModal from '../components/ConfirmModal.vue'
import ToolsChecklist from '../components/ToolsChecklist.vue'
import DriftChip from '../components/DriftChip.vue'
import DriftDrawer from '../components/DriftDrawer.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import KPICard from '../components/KPICard.vue'
import ResourceGauge from '../components/ResourceGauge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import type { Status } from '../components/types'
import { STATUS_LABEL, absoluteTime, relativeTime, statusDot } from '../lib/servers'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useJobsStore } from '../stores/jobs'
import { onUnmounted } from 'vue'

type RowStatus = 'pending' | 'running' | 'ok' | 'fail' | 'skipped'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const jobsStore = useJobsStore()
const canManage = auth.hasPermission('server:manage')

const serverId = Number(route.params.id)

// ── Server detail tabs ─────────────────────────────────────────────────────
type ServerTabKey = 'overview' | 'packages'
const serverTabs: { key: ServerTabKey; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'packages', label: 'Packages & Tools' },
]
const serverTab = ref<ServerTabKey>('overview')
const launching = ref(false)
const mariadbPassword = ref('')
const savingDbPw = ref(false)
const server = ref<Server | null>(null)
const loading = ref(true)
const loadError = ref('')

// -- Config drift (session 6.7) -----------------------------------------------
const driftBaselines = ref<DriftBaseline[]>([])
const driftLoading = ref(false)
const activeDriftId = ref<number | null>(null)
const runningDriftCheck = ref(false)

const driftedCount = computed(() => driftBaselines.value.filter((b) => b.status === 'drifted').length)

async function loadDrift() {
  driftLoading.value = true
  try {
    driftBaselines.value = await driftApi.list({ server_id: serverId })
  } catch {
    // Non-fatal — drift section shows empty state
  } finally {
    driftLoading.value = false
  }
}

async function triggerDriftCheck() {
  if (runningDriftCheck.value) return
  runningDriftCheck.value = true
  try {
    const { job_id } = await driftApi.runCheck(serverId)
    router.push(`/jobs/${job_id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A drift check is already running on this server.'
        : error instanceof Error
          ? error.message
          : 'Could not start drift check.'
    toast.error(message)
  } finally {
    runningDriftCheck.value = false
  }
}

// -- Per-server rollup (session 2.6, B4.2) -----------------------------------
const dashboard = ref<ServerDashboard | null>(null)

async function loadDashboard() {
  try {
    dashboard.value = await serversApi.dashboard(serverId)
  } catch {
    dashboard.value = null // the rollup strip simply hides on error
  }
}

function fmtBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`
}

const rollupCards = computed(() => {
  const d = dashboard.value
  if (!d) return []
  const capacityStatus =
    d.capacity == null
      ? 'muted'
      : !d.capacity.ok
        ? 'err'
        : (d.capacity.disk_pct ?? 0) >= 90 || (d.capacity.mem_pct ?? 0) >= 90
          ? 'warn'
          : 'ok'
  return [
    {
      label: 'Sites up',
      value: `${d.sites.up}/${d.sites.total}`,
      status: (d.sites.down > 0 ? 'warn' : 'ok') as Status,
      sublabel: `${d.benches} bench${d.benches === 1 ? '' : 'es'}`,
    },
    {
      label: 'Capacity',
      value: d.capacity?.cpu_pct != null ? `${Math.round(d.capacity.cpu_pct)}% CPU` : 'No data',
      status: capacityStatus as Status,
      sublabel:
        d.capacity?.mem_pct != null && d.capacity?.disk_pct != null
          ? `RAM ${Math.round(d.capacity.mem_pct)}% · Disk ${Math.round(d.capacity.disk_pct)}%`
          : 'Awaiting a poll',
    },
    {
      label: 'Jobs (24h)',
      value: String(d.jobs_24h.total),
      status: (d.jobs_24h.failure > 0 ? 'err' : 'ok') as Status,
      sublabel: `${d.jobs_24h.success} ok · ${d.jobs_24h.failure} failed`,
    },
    {
      label: 'Backups',
      value: String(d.backups.count),
      status: 'ok' as Status,
      sublabel: `${fmtBytes(d.backups.total_size_bytes)}${d.backups.last_backup_at ? ` · last ${relativeTime(d.backups.last_backup_at)}` : ''}`,
    },
  ]
})

const TOOL_KEYS =['git', 'python3', 'uv', 'node', 'mariadb', 'redis-server', 'wkhtmltopdf', 'bench']
const coreRows = reactive<{ key: string; label: string; status: RowStatus; value: string | null }[]>([])
const toolRows = reactive<{ key: string; label: string; status: RowStatus; value: string | null }[]>([])

const testing = ref(false)
const hasRun = ref(false)
const testError = ref('')

// -- Live monitoring ---------------------------------------------------------
const MON_POLL_MS = 15000
const latest = ref<MonitoringSample | null>(null)
const monLoading = ref(true)
const monError = ref('')
const restarting = ref<ServiceName | null>(null)
const restartModalOpen = ref(false)
const pendingService = ref<ServiceName | null>(null)
let monTimer: ReturnType<typeof setInterval> | null = null

function serviceState(key: ServiceName): ServiceState {
  return (latest.value?.services?.[key] as ServiceState) ?? 'unknown'
}

function serviceDot(state: ServiceState): Status {
  if (state === 'active') return 'ok'
  if (state === 'failed') return 'err'
  if (state === 'inactive') return 'warn'
  return 'muted'
}

async function loadMonitoring(initial = false) {
  if (initial) monLoading.value = true
  try {
    const res = await monitoringApi.get(serverId)
    latest.value = res.latest
    monError.value = ''
  } catch (error) {
    monError.value = error instanceof Error ? error.message : 'Could not load monitoring.'
  } finally {
    monLoading.value = false
  }
}

function askRestart(service: ServiceName) {
  pendingService.value = service
  restartModalOpen.value = true
}

async function confirmRestart() {
  const service = pendingService.value
  if (!service || restarting.value) return
  restarting.value = service
  try {
    const job = await monitoringApi.restartService(serverId, service)
    jobsStore.merge(job)
    restartModalOpen.value = false
    pendingService.value = null
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this server.'
        : error instanceof Error
          ? error.message
          : `Could not restart ${service}.`
    toast.error(message)
  } finally {
    restarting.value = null
  }
}

const specs = computed(() => {
  const s = server.value
  if (!s) return []
  return [
    { label: 'Status', value: STATUS_LABEL[s.status] },
    { label: 'Hostname / IP', value: s.hostname },
    { label: 'SSH port', value: String(s.ssh_port) },
    { label: 'Operating system', value: s.os_version ?? '—' },
    { label: 'SSH user', value: s.credential?.username ?? '—' },
    { label: 'Auth', value: s.credential ? authLabel(s.credential.auth_type) : '—' },
    { label: 'Sudo mode', value: s.credential?.sudo_mode === 'nopasswd' ? 'Passwordless' : 'None' },
    { label: 'Host key pinned', value: s.credential?.host_key_pinned ? 'Yes' : 'No' },
    { label: 'Tags', value: s.tags.length ? s.tags.join(', ') : '—' },
    { label: 'Last seen', value: `${relativeTime(s.last_seen)} (${absoluteTime(s.last_seen) || 'never'})` },
  ]
})

function authLabel(type: string) {
  return type === 'password' ? 'Password' : 'SSH key'
}

function dotOf(status: RowStatus): Status {
  if (status === 'ok') return 'ok'
  if (status === 'fail') return 'err'
  return 'muted'
}

function seedRows() {
  coreRows.splice(0, coreRows.length,
    { key: 'ssh', label: 'SSH connection', status: 'pending', value: null },
    { key: 'whoami', label: 'Login user', status: 'pending', value: null },
    { key: 'sudo', label: 'Passwordless sudo', status: 'pending', value: null },
    { key: 'os', label: 'Operating system', status: 'pending', value: null },
  )
  toolRows.splice(0, toolRows.length,
    ...TOOL_KEYS.map((t) => ({ key: `tool:${t}`, label: t, status: 'pending' as RowStatus, value: null })),
  )
}

function applyEvent(ev: CheckEvent) {
  if (ev.check === 'done' || ev.check === 'error') {
    if (ev.check === 'error') testError.value = ev.error ?? 'The test reported an error.'
    return
  }
  const key = ev.check === 'tool' ? `tool:${ev.name}` : ev.check
  const row = [...coreRows, ...toolRows].find((r) => r.key === key)
  if (row) {
    row.status = ev.ok ? 'ok' : 'fail'
    row.value = ev.value ?? null
  }
}

async function runTest() {
  if (testing.value) return
  testing.value = true
  hasRun.value = true
  testError.value = ''
  seedRows()
  coreRows[0].status = 'running'
  try {
    await streamServerTest(serverId, applyEvent)
    server.value = await serversApi.get(serverId) // refresh status/os/last_seen
  } catch (error) {
    testError.value = error instanceof Error ? error.message : 'The connection test failed to start.'
  } finally {
    for (const row of [...coreRows, ...toolRows]) {
      if (row.status === 'pending' || row.status === 'running') row.status = 'skipped'
    }
    testing.value = false
  }
}

// Launch the harmless three-step demo action against this server and jump
// straight to its live detail page — the end-to-end proof of the job engine.
async function runDemoJob() {
  if (launching.value) return
  launching.value = true
  try {
    const job = await jobsApi.create({
      action_name: 'system.echo_demo',
      server_id: serverId,
      params: { message: 'Hello from FDM' },
    })
    jobsStore.merge(job)
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A demo job is already running on this server.'
        : error instanceof Error
          ? error.message
          : 'Could not start the demo job.'
    toast.error(message)
  } finally {
    launching.value = false
  }
}

async function saveMariadbPassword() {
  if (savingDbPw.value || !mariadbPassword.value) return
  savingDbPw.value = true
  try {
    await serversApi.update(serverId, { mariadb_root_password: mariadbPassword.value })
    mariadbPassword.value = ''
    server.value = await serversApi.get(serverId)
    toast.success('MariaDB root password saved.')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not save the password.')
  } finally {
    savingDbPw.value = false
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    server.value = await serversApi.get(serverId)
    seedRows()
    void loadDashboard()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load this server.'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load()
  void loadMonitoring(true)
  void loadDrift()
  monTimer = setInterval(() => void loadMonitoring(), MON_POLL_MS)
})

onUnmounted(() => {
  if (monTimer !== null) clearInterval(monTimer)
})
</script>
