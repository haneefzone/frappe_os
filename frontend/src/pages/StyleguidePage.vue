<template>
  <div class="mx-auto max-w-6xl space-y-10 px-8 py-6">
    <header class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-page font-semibold text-ink-1">Styleguide</h1>
        <p class="mt-1 text-body text-ink-2">
          Every core component with fake data. Toggle the theme to check both modes.
        </p>
      </div>
      <Button
        variant="outline"
        theme="gray"
        :label="theme === 'dark' ? 'Switch to light' : 'Switch to dark'"
        @click="toggle"
      />
    </header>

    <!-- ============ Tokens ============ -->
    <section>
      <h2 class="mb-3 text-section font-semibold text-ink-1">Design tokens</h2>
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
        <div
          v-for="token in tokenSwatches"
          :key="token.name"
          class="rounded-lg border border-line bg-surface p-2.5"
        >
          <div class="h-10 rounded border border-line" :style="{ background: token.value }" />
          <p class="mt-2 font-mono text-meta text-ink-1">{{ token.name }}</p>
          <p class="font-mono text-meta text-ink-3">{{ token.hex }}</p>
        </div>
      </div>
      <div class="mt-4 space-y-1 rounded-lg border border-line bg-surface p-4">
        <p class="text-meta text-ink-3">12px meta — timestamps, table headers</p>
        <p class="text-label text-ink-2">13px secondary — table cells, helper text</p>
        <p class="text-body text-ink-1">14px body — default UI text</p>
        <p class="text-section font-semibold text-ink-1">16px section title</p>
        <p class="text-page font-semibold text-ink-1">20px page title</p>
        <p class="text-kpi font-semibold text-ink-1">28 <span class="text-body font-normal text-ink-3">KPI figures</span></p>
        <p class="font-mono text-label text-ink-2">JetBrains Mono — logs, terminal, commands</p>
      </div>
    </section>

    <!-- ============ Status ============ -->
    <section>
      <h2 class="mb-3 text-section font-semibold text-ink-1">StatusDot · StatusBadge · EnvironmentBadge</h2>
      <div class="space-y-4 rounded-lg border border-line bg-surface p-4">
        <div class="flex flex-wrap items-center gap-5">
          <span v-for="s in statuses" :key="s" class="flex items-center gap-2 text-label text-ink-2">
            <StatusDot :status="s" /> {{ s }}
          </span>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <StatusBadge status="ok" />
          <StatusBadge status="warn" label="SSL expires in 12d" />
          <StatusBadge status="err" label="Scheduler stopped" />
          <StatusBadge status="running" label="Backup running" />
          <StatusBadge status="muted" label="Maintenance mode" />
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <EnvironmentBadge env="prod" />
          <EnvironmentBadge env="staging" />
          <EnvironmentBadge env="dev" />
        </div>
      </div>
    </section>

    <!-- ============ KPI cards ============ -->
    <section>
      <h2 class="mb-3 text-section font-semibold text-ink-1">KPICard · Sparkline</h2>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KPICard label="Fleet health" value="98%" sublabel="21 of 22 sites healthy" status="ok" />
        <KPICard
          label="Backup compliance"
          value="87%"
          delta="-6%"
          delta-tone="err"
          sublabel="3 sites overdue"
          status="warn"
        />
        <KPICard label="Jobs · 24h" value="142" delta="+12" delta-tone="ok" sublabel="2 failed" status="err">
          <template #footer>
            <Sparkline :data="jobSparkline" status="info" :width="200" :height="28" filled />
          </template>
        </KPICard>
        <KPICard label="Open alerts" loading />
      </div>
      <div class="mt-3 flex flex-wrap items-end gap-6 rounded-lg border border-line bg-surface p-4">
        <div v-for="v in sparklineVariants" :key="v.label">
          <p class="mb-1 text-meta text-ink-3">{{ v.label }}</p>
          <Sparkline :data="v.data" :status="v.status" :filled="v.filled" :show-last="v.showLast" />
        </div>
      </div>
    </section>

    <!-- ============ DataTable ============ -->
    <section>
      <h2 class="mb-1 text-section font-semibold text-ink-1">DataTable</h2>
      <p class="mb-3 text-label text-ink-2">
        1,000 generated site rows — sortable columns, text filter, column chooser, density toggle,
        sticky header, row actions. The toggles below show the loading and empty states.
      </p>
      <div class="mb-2 flex gap-2">
        <Button variant="outline" theme="gray" size="sm" :label="tableLoading ? 'Stop loading demo' : 'Show loading state'" @click="tableLoading = !tableLoading" />
        <Button variant="outline" theme="gray" size="sm" :label="tableEmpty ? 'Restore rows' : 'Show empty state'" @click="tableEmpty = !tableEmpty" />
      </div>
      <DataTable
        :columns="siteColumns"
        :rows="tableEmpty ? [] : siteRows"
        row-key="name"
        :loading="tableLoading"
        height="420px"
        filter-placeholder="Filter sites"
        empty-title="No sites yet"
        empty-message="Create a site on one of your benches to see it here."
      >
        <template #cell-status="{ row }">
          <StatusBadge :status="(row.status as Status)" :label="String(row.statusLabel)" />
        </template>
        <template #cell-env="{ row }">
          <EnvironmentBadge :env="(row.env as Environment)" />
        </template>
        <template #actions="{ row }">
          <Button
            variant="ghost"
            theme="gray"
            size="sm"
            label="Backup"
            @click="toast.success(`Backup queued for ${row.name}`, { title: 'Job queued' })"
          />
        </template>
      </DataTable>
    </section>

    <!-- ============ Modals ============ -->
    <section>
      <h2 class="mb-1 text-section font-semibold text-ink-1">ConfirmModal</h2>
      <p class="mb-3 text-label text-ink-2">
        The destructive variant keeps its red button disabled until you type
        <span class="font-mono text-ink-1">prod.acme.com</span> exactly.
      </p>
      <div class="flex gap-2">
        <Button variant="solid" theme="gray" label="Restart workers…" @click="standardModal = true" />
        <Button variant="solid" theme="red" label="Drop site…" @click="destructiveModal = true" />
      </div>

      <ConfirmModal
        v-model="standardModal"
        title="Restart workers"
        message="Background jobs pause for a few seconds while workers restart."
        verb="Restart workers"
        :consequences="['All RQ workers on srv-app-01 restart', 'Running jobs finish before restart']"
        @confirm="onStandardConfirm"
      />

      <ConfirmModal
        v-model="destructiveModal"
        variant="destructive"
        title="Drop site prod.acme.com"
        message="This permanently deletes the site, its database, and all files."
        verb="Drop site prod.acme.com"
        target-name="prod.acme.com"
        :consequences="[
          'Delete the MariaDB database (4.2 GB)',
          'Delete public and private files (11.8 GB)',
          'Remove the site from bench prod-bench-v16',
          'Invalidate all active user sessions',
        ]"
        @confirm="onDestructiveConfirm"
      >
        <template #backup>
          <p>A full backup (database + files) runs and must succeed before the drop starts.</p>
        </template>
      </ConfirmModal>
    </section>

    <!-- ============ Wizard ============ -->
    <section>
      <h2 class="mb-3 text-section font-semibold text-ink-1">Wizard</h2>
      <Wizard
        v-model="wizardStep"
        :steps="wizardSteps"
        submit-label="Create bench"
        :can-continue="wizardCanContinue"
        @submit="onWizardSubmit"
      >
        <template #step-server>
          <div class="space-y-2">
            <label
              v-for="server in fakeServers"
              :key="server.name"
              class="flex cursor-pointer items-center gap-3 rounded-lg border p-3 transition"
              :class="wizard.server === server.name ? 'border-line-strong bg-raised' : 'border-line hover:bg-raised'"
            >
              <input v-model="wizard.server" type="radio" :value="server.name" class="h-3.5 w-3.5" />
              <StatusDot :status="server.status" />
              <span class="text-body font-medium text-ink-1">{{ server.name }}</span>
              <EnvironmentBadge :env="server.env" />
              <span class="ml-auto text-label text-ink-3">{{ server.ip }}</span>
            </label>
          </div>
        </template>
        <template #step-version>
          <div class="grid gap-2 sm:grid-cols-3">
            <label
              v-for="v in versionMatrix"
              :key="v.version"
              class="flex cursor-pointer flex-col gap-1 rounded-lg border p-3 transition"
              :class="wizard.version === v.version ? 'border-line-strong bg-raised' : 'border-line hover:bg-raised'"
            >
              <span class="flex items-center gap-2">
                <input v-model="wizard.version" type="radio" :value="v.version" class="h-3.5 w-3.5" />
                <span class="text-body font-semibold text-ink-1">Frappe {{ v.version }}</span>
              </span>
              <span class="text-meta text-ink-3">
                Python {{ v.python }} · Node {{ v.node }} · MariaDB {{ v.mariadb }}
              </span>
            </label>
          </div>
        </template>
        <template #step-name>
          <label class="mb-1 block text-label text-ink-2">Bench name</label>
          <input
            v-model="wizard.name"
            type="text"
            placeholder="prod-bench-v16"
            class="fdm-focus w-full max-w-sm rounded-lg border border-line bg-base px-2.5 py-1.5 font-mono text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
          />
          <p class="mt-1 text-meta text-ink-3">Lowercase letters, numbers, and dashes.</p>
        </template>
        <template #step-review>
          <dl class="space-y-2 text-label">
            <div class="flex gap-3"><dt class="w-24 text-ink-3">Server</dt><dd class="text-ink-1">{{ wizard.server }}</dd></div>
            <div class="flex gap-3"><dt class="w-24 text-ink-3">Version</dt><dd class="text-ink-1">Frappe {{ wizard.version }}</dd></div>
            <div class="flex gap-3"><dt class="w-24 text-ink-3">Name</dt><dd class="font-mono text-ink-1">{{ wizard.name }}</dd></div>
          </dl>
          <details class="mt-3 rounded-lg border border-line bg-base px-3 py-2">
            <summary class="cursor-pointer text-label text-ink-2">Show exact commands</summary>
            <pre class="mt-2 overflow-x-auto font-mono text-meta text-ink-2">bench init --frappe-branch version-{{ wizard.version?.replace('v', '') }} {{ wizard.name }}</pre>
          </details>
        </template>
      </Wizard>
    </section>

    <!-- ============ EmptyState / Toast / CopyField ============ -->
    <div class="grid gap-6 lg:grid-cols-2">
      <section>
        <h2 class="mb-3 text-section font-semibold text-ink-1">EmptyState</h2>
        <div class="rounded-lg border border-line bg-surface">
          <EmptyState
            :icon="LucideServer"
            title="No servers connected"
            message="Add your first Ubuntu server over SSH to start managing benches."
            cta-label="Add server"
            @cta="toast.info('This would open the Add Server wizard.')"
          />
        </div>
      </section>

      <section class="space-y-6">
        <div>
          <h2 class="mb-3 text-section font-semibold text-ink-1">Toast service</h2>
          <div class="flex flex-wrap gap-2 rounded-lg border border-line bg-surface p-4">
            <Button variant="outline" theme="gray" label="Info" @click="toast.info('Connected to srv-app-01.')" />
            <Button variant="outline" theme="gray" label="Success" @click="toast.success('Backup of prod.acme.com finished.', { title: 'Job complete' })" />
            <Button variant="outline" theme="gray" label="Warning" @click="toast.warning('Disk on srv-db-01 is 82% full.')" />
            <Button variant="outline" theme="gray" label="Error" @click="toast.error('bench update failed on step Migrate sites.', { title: 'Job failed' })" />
          </div>
        </div>
        <div>
          <h2 class="mb-3 text-section font-semibold text-ink-1">CopyField</h2>
          <div class="space-y-3 rounded-lg border border-line bg-surface p-4">
            <CopyField label="API token (masked)" value="fdm_live_9f8a7b6c5d4e3f2a1b0c" secret />
            <CopyField
              label="Add to authorized_keys"
              value="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF7f3… fdm-platform"
            />
          </div>
        </div>
      </section>
    </div>

    <!-- ============ JobTimeline ============ -->
    <section>
      <h2 class="mb-3 text-section font-semibold text-ink-1">JobTimeline</h2>
      <div class="grid gap-3 lg:grid-cols-2">
        <div class="rounded-lg border border-line bg-surface p-4">
          <p class="mb-3 text-label font-medium text-ink-1">Create site — running</p>
          <JobTimeline :steps="runningJob" />
        </div>
        <div class="rounded-lg border border-line bg-surface p-4">
          <p class="mb-3 text-label font-medium text-ink-1">bench update — failed (expand the error)</p>
          <JobTimeline :steps="failedJob" />
        </div>
      </div>
    </section>

    <!-- ============ LogViewer ============ -->
    <section>
      <h2 class="mb-1 text-section font-semibold text-ink-1">LogViewer</h2>
      <p class="mb-3 text-label text-ink-2">
        Search filters and highlights; error lines are tinted red; follow-tail sticks to the bottom
        as the fake job appends lines. Try searching for “redis”.
      </p>
      <div class="mb-2">
        <Button
          variant="outline"
          theme="gray"
          size="sm"
          :label="streaming ? 'Stop appending lines' : 'Append lines (test follow-tail)'"
          @click="toggleStreaming"
        />
      </div>
      <LogViewer :lines="logLines" title="job-1042 · bench update" filename="job-1042.log" initial-follow />
    </section>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import LucideServer from '~icons/lucide/server'
import ConfirmModal from '../components/ConfirmModal.vue'
import CopyField from '../components/CopyField.vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import JobTimeline from '../components/JobTimeline.vue'
import KPICard from '../components/KPICard.vue'
import LogViewer from '../components/LogViewer.vue'
import Sparkline from '../components/Sparkline.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import Wizard from '../components/Wizard.vue'
import type { DataTableColumn, Environment, JobStep, Status, WizardStep } from '../components/types'
import { useTheme } from '../composables/useTheme'

const { theme, toggle } = useTheme()

/* ---------- Tokens ---------- */
const tokenSwatches = [
  { name: '--bg-base', value: 'var(--bg-base)', hex: '#0A0A0B / #FAFAFA' },
  { name: '--bg-surface', value: 'var(--bg-surface)', hex: '#111113 / #FFFFFF' },
  { name: '--bg-raised', value: 'var(--bg-raised)', hex: '#17171A / #F4F4F5' },
  { name: '--border', value: 'var(--border)', hex: '#232326 / #E4E4E7' },
  { name: '--border-strong', value: 'var(--border-strong)', hex: '#2E2E33 / #D4D4D8' },
  { name: '--text-primary', value: 'var(--text-primary)', hex: '#F4F4F5 / #18181B' },
  { name: '--text-secondary', value: 'var(--text-secondary)', hex: '#A1A1AA / #52525B' },
  { name: '--text-muted', value: 'var(--text-muted)', hex: '#6B6B74 / #8E8E99' },
  { name: 'ok', value: '#22C55E', hex: '#22C55E' },
  { name: 'warn', value: '#F59E0B', hex: '#F59E0B' },
  { name: 'err', value: '#EF4444', hex: '#EF4444' },
  { name: 'running / info', value: '#3B82F6', hex: '#3B82F6' },
]

const statuses: Status[] = ['ok', 'warn', 'err', 'running', 'muted']

/* ---------- Fake data (seeded so every reload looks the same) ---------- */
function mulberry32(seed: number) {
  return () => {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const rand = mulberry32(42)
const pick = <T,>(arr: T[]) => arr[Math.floor(rand() * arr.length)]

const jobSparkline = Array.from({ length: 24 }, () => Math.round(2 + rand() * 10))
const sparklineVariants = [
  { label: 'Monochrome', data: Array.from({ length: 20 }, () => rand() * 10), status: undefined, filled: false, showLast: false },
  { label: 'ok · filled', data: Array.from({ length: 20 }, (_, i) => 5 + i * 0.3 + rand() * 2), status: 'ok' as const, filled: true, showLast: true },
  { label: 'warn', data: Array.from({ length: 20 }, () => 4 + rand() * 6), status: 'warn' as const, filled: false, showLast: true },
  { label: 'err · filled', data: Array.from({ length: 20 }, (_, i) => 10 - i * 0.3 + rand() * 2), status: 'err' as const, filled: true, showLast: false },
  { label: 'info', data: Array.from({ length: 20 }, () => 2 + rand() * 8), status: 'info' as const, filled: false, showLast: false },
]

interface SiteRow extends Record<string, unknown> {
  name: string
  server: string
  bench: string
  env: Environment
  version: string
  status: Status
  statusLabel: string
  responseMs: number
  lastBackup: string
}

const SERVERS = ['srv-app-01', 'srv-app-02', 'srv-db-01', 'srv-edge-01']
const WORDS = ['acme', 'globex', 'initech', 'umbrella', 'stark', 'wayne', 'hooli', 'wonka', 'cyberdyne', 'tyrell']
const TLDS = ['com', 'io', 'net', 'co', 'org']
const STATUS_POOL: Array<{ status: Status; label: string }> = [
  { status: 'ok', label: 'Healthy' },
  { status: 'ok', label: 'Healthy' },
  { status: 'ok', label: 'Healthy' },
  { status: 'warn', label: 'Backup overdue' },
  { status: 'err', label: 'Down' },
  { status: 'running', label: 'Updating' },
  { status: 'muted', label: 'Maintenance' },
]

const siteRows: SiteRow[] = Array.from({ length: 1000 }, (_, i) => {
  const env: Environment = rand() < 0.25 ? 'prod' : rand() < 0.4 ? 'staging' : 'dev'
  const s = pick(STATUS_POOL)
  const version = pick(['v14', 'v15', 'v16'])
  return {
    name: `${env === 'prod' ? '' : env + '.'}${pick(WORDS)}-${i + 1}.${pick(TLDS)}`,
    server: pick(SERVERS),
    bench: `${env}-bench-${version}`,
    env,
    version,
    status: s.status,
    statusLabel: s.label,
    responseMs: Math.round(40 + rand() * 900),
    lastBackup: `${Math.floor(rand() * 48)}h ago`,
  }
})

const siteColumns: DataTableColumn<SiteRow>[] = [
  { key: 'name', label: 'Site', sortable: true },
  { key: 'status', label: 'Status', sortable: true },
  { key: 'env', label: 'Env', sortable: true },
  { key: 'server', label: 'Server', sortable: true },
  { key: 'bench', label: 'Bench', sortable: true, hidden: true },
  { key: 'version', label: 'Version', sortable: true },
  { key: 'responseMs', label: 'Resp (ms)', sortable: true, align: 'right' },
  { key: 'lastBackup', label: 'Last backup', sortable: true },
]

const tableLoading = ref(false)
const tableEmpty = ref(false)

/* ---------- Modals ---------- */
const standardModal = ref(false)
const destructiveModal = ref(false)

function onStandardConfirm() {
  standardModal.value = false
  toast.success('Worker restart queued on srv-app-01.', { title: 'Job queued' })
}
function onDestructiveConfirm() {
  destructiveModal.value = false
  toast.success('Pre-drop backup started; the drop runs after it succeeds.', { title: 'Job queued' })
}

/* ---------- Wizard ---------- */
const wizardSteps: WizardStep[] = [
  { key: 'server', label: 'Server', description: 'Where should the bench live?' },
  { key: 'version', label: 'Version', description: 'Each Frappe version pins its own toolchain.' },
  { key: 'name', label: 'Name' },
  { key: 'review', label: 'Review', description: 'Nothing runs until you create the bench.' },
]
const wizardStep = ref(0)
const wizard = reactive<{ server: string | null; version: string | null; name: string }>({
  server: null,
  version: null,
  name: '',
})
const fakeServers = [
  { name: 'srv-app-01', env: 'prod' as const, ip: '10.0.1.10', status: 'ok' as const },
  { name: 'srv-app-02', env: 'staging' as const, ip: '10.0.1.11', status: 'ok' as const },
  { name: 'srv-db-01', env: 'dev' as const, ip: '10.0.2.20', status: 'warn' as const },
]
const versionMatrix = [
  { version: 'v14', python: '3.10', node: '16–18', mariadb: '10.6+' },
  { version: 'v15', python: '3.11–3.12', node: '18–20', mariadb: '10.6+' },
  { version: 'v16', python: '3.14', node: '24', mariadb: '11.8' },
]
const wizardCanContinue = computed(() => {
  if (wizardStep.value === 0) return wizard.server !== null
  if (wizardStep.value === 1) return wizard.version !== null
  if (wizardStep.value === 2) return /^[a-z0-9][a-z0-9-]{2,}$/.test(wizard.name)
  return true
})
function onWizardSubmit() {
  toast.success(`Bench ${wizard.name} queued on ${wizard.server}.`, { title: 'Job queued' })
}

/* ---------- JobTimeline ---------- */
const runningJob: JobStep[] = [
  { label: 'Validate prerequisites', status: 'done', duration: '2s' },
  { label: 'Create database', status: 'done', duration: '14s' },
  { label: 'Install frappe', status: 'running', startedAt: Date.now() - 47_000 },
  { label: 'Install erpnext', status: 'pending' },
  { label: 'Enable scheduler', status: 'pending' },
]
const failedJob: JobStep[] = [
  { label: 'Pre-update backup', status: 'done', duration: '1m 12s' },
  { label: 'Pull app updates', status: 'done', duration: '38s' },
  {
    label: 'Migrate sites',
    status: 'failed',
    error:
      'Traceback (most recent call last):\n  File "apps/frappe/frappe/migrate.py", line 82, in migrate\n    frappe.db.updatedb(doctype)\npymysql.err.OperationalError: (1054, "Unknown column \'custom_field\' in \'field list\'")',
  },
  { label: 'Rebuild assets', status: 'pending' },
]

/* ---------- LogViewer ---------- */
const BASE_LOG: string[] = (() => {
  const lines: string[] = []
  const apps = ['frappe', 'erpnext', 'hrms']
  for (let i = 0; i < 260; i++) {
    const r = rand()
    if (r < 0.04) lines.push(`ERROR 2026-07-07 10:${String(i % 60).padStart(2, '0')}:12 worker.high: job failed after 3 retries`)
    else if (r < 0.06) lines.push(`Warning: redis cache at 127.0.0.1:13000 responded slowly (412ms)`)
    else if (r < 0.09) lines.push(`$ bench --site all migrate --skip-failing`)
    else lines.push(`INFO 2026-07-07 10:${String(i % 60).padStart(2, '0')}:${String((i * 7) % 60).padStart(2, '0')} ${pick(apps)}: patch ${pick(apps)}.patches.v${pick(['14', '15', '16'])}_0.${Math.floor(rand() * 90)} completed`)
  }
  lines.push('Traceback (most recent call last):')
  lines.push('  File "apps/frappe/frappe/utils/bench_helper.py", line 91, in main')
  lines.push('pymysql.err.OperationalError: (2003, "Can\'t connect to MySQL server on \'127.0.0.1\'")')
  return lines
})()

const logLines = ref<string[]>([...BASE_LOG])
const streaming = ref(false)
let streamTimer: number | undefined

function toggleStreaming() {
  streaming.value = !streaming.value
  if (streaming.value) {
    streamTimer = window.setInterval(() => {
      logLines.value = [
        ...logLines.value,
        `INFO ${new Date().toISOString()} live: appended line ${logLines.value.length + 1}`,
      ]
    }, 800)
  } else if (streamTimer !== undefined) {
    window.clearInterval(streamTimer)
    streamTimer = undefined
  }
}
onBeforeUnmount(() => {
  if (streamTimer !== undefined) window.clearInterval(streamTimer)
})
</script>
