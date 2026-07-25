<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center gap-3 border-b border-line px-8 py-5">
      <button
        type="button"
        class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
        aria-label="Back to benches"
        @click="router.push('/benches')"
      >
        <LucideArrowLeft class="h-4 w-4" />
      </button>
      <StatusDot v-if="bench" :status="benchDot(bench.status)" />
      <h1 class="truncate text-lg font-semibold text-ink-1">{{ bench?.name ?? 'Bench' }}</h1>
      <StatusBadge
        v-if="bench"
        :status="bench.is_production ? 'running' : 'muted'"
        :label="bench.is_production ? 'Production' : 'Dev'"
      />
    </header>

    <!-- Tab bar (skeleton: only Overview is wired this session). -->
    <nav class="flex gap-1 border-b border-line px-8" aria-label="Bench sections">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :disabled="tab.key !== 'overview'"
        class="fdm-focus -mb-px border-b-2 px-3 py-2.5 text-label transition"
        :class="tab.key === activeTab
          ? 'border-ink-1 font-medium text-ink-1'
          : 'border-transparent text-ink-3 disabled:opacity-40'"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>

      <div v-else-if="loading" class="max-w-3xl rounded-lg border border-line bg-surface">
        <div class="border-b border-line px-4 py-2.5"><div class="h-3.5 w-24 animate-pulse rounded bg-raised" /></div>
        <div class="divide-y divide-line">
          <div v-for="i in 8" :key="i" class="flex items-center justify-between px-4 py-2">
            <div class="h-3 w-28 animate-pulse rounded bg-raised" />
            <div class="h-3 w-40 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>

      <div v-else-if="bench && activeTab === 'overview'" class="grid max-w-4xl gap-6 lg:grid-cols-2">
        <!-- Parsed config: identity + versions -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Overview</h2>
          <dl class="divide-y divide-line">
            <div v-for="spec in specs" :key="spec.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ spec.label }}</dt>
              <dd class="max-w-[62%] truncate text-label text-ink-1" :title="spec.value">{{ spec.value }}</dd>
            </div>
          </dl>
        </section>

        <!-- Parsed config: port map from common_site_config.json -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Ports</h2>
          <dl class="divide-y divide-line">
            <div v-for="p in portList" :key="p.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ p.label }}</dt>
              <dd class="font-mono text-label text-ink-1">{{ p.value ?? '—' }}</dd>
            </div>
          </dl>
          <p class="px-4 py-2 text-meta text-ink-3">Parsed from sites/common_site_config.json.</p>
        </section>

        <!-- Actions (session 1.10) — each launches a job you land on -->
        <section
          v-if="bench.status === 'active' && (canOperate || canMigrate)"
          class="rounded-lg border border-line bg-surface lg:col-span-2"
        >
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Actions</h2>
          <div class="flex flex-wrap items-center gap-2 p-4">
            <Button
              v-if="canOperate"
              variant="subtle"
              theme="gray"
              label="Build assets"
              :disabled="launching"
              @click="askBench('build')"
            />
            <Button
              v-if="canOperate"
              variant="subtle"
              theme="gray"
              label="Restart"
              :disabled="launching"
              @click="askBench('restart')"
            />
            <Button
              v-if="canMigrate"
              variant="subtle"
              theme="gray"
              label="Migrate all sites"
              :disabled="launching"
              @click="askBench('migrate-all')"
            />
            <Button
              v-if="canOperate"
              variant="solid"
              theme="gray"
              label="Update bench"
              :disabled="launching"
              @click="askBench('update')"
            />
            <Button
              v-if="canOperate && !bench.is_production"
              variant="solid"
              theme="red"
              label="Set up production"
              :disabled="launching"
              @click="askBench('setup-production')"
            />
          </div>
          <p class="px-4 pb-3 text-meta text-ink-3">
            Each action runs as a job — you'll land on its live log.
          </p>
        </section>

        <!-- Config drift for this bench (session 6.7) -->
        <section class="rounded-lg border border-line bg-surface lg:col-span-2">
          <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
            <h2 class="text-label font-semibold text-ink-1">Config drift</h2>
            <span v-if="benchDriftBaselines.length" class="text-meta text-ink-3">
              {{ benchDriftBaselines.filter(b => b.status === 'drifted').length }} drifted of {{ benchDriftBaselines.length }}
            </span>
          </div>
          <div v-if="driftLoading" class="flex flex-wrap gap-2 p-4">
            <div v-for="i in 3" :key="i" class="h-6 w-28 animate-pulse rounded-full bg-raised" />
          </div>
          <div v-else-if="benchDriftBaselines.length === 0" class="px-4 py-3 text-label text-ink-3">
            No config baselines tracked for this bench yet.
          </div>
          <div v-else class="flex flex-wrap gap-2 p-4">
            <DriftChip
              v-for="b in benchDriftBaselines"
              :key="b.id"
              :baseline="b"
              @click="activeDriftId = b.id"
            />
          </div>
        </section>
      </div>
    </div>

  <DriftDrawer
    :baseline-id="activeDriftId"
    @close="activeDriftId = null"
    @accepted="loadBenchDrift"
  />

    <!-- Maintenance confirm (build / restart / migrate-all / update / set up production) -->
    <ConfirmModal
      v-model="benchOpen"
      :title="benchConfig.title"
      :message="benchConfig.message"
      :verb="benchConfig.verb"
      :variant="benchConfig.variant"
      :target-name="benchConfig.targetName"
      :consequences="benchConfig.consequences"
      :backup-notice="benchConfig.backupNotice"
      :loading="launching"
      @confirm="confirmBench"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import { benchesApi, type Bench } from '../api/benches'
import { ApiError } from '../api/client'
import { driftApi, type DriftBaseline } from '../api/drift'
import ConfirmModal from '../components/ConfirmModal.vue'
import DriftChip from '../components/DriftChip.vue'
import DriftDrawer from '../components/DriftDrawer.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import { benchStatusDot as benchDot, portRows, versionChip } from '../lib/benches'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const benchId = Number(route.params.id)
const auth = useAuthStore()
const canOperate = auth.hasPermission('bench:operate')
const canMigrate = auth.hasPermission('site:operate')

const bench = ref<Bench | null>(null)
const loading = ref(true)
const loadError = ref('')
const activeTab = ref('overview')

// -- Config drift (session 6.7) -----------------------------------------------
const allServerDrift = ref<DriftBaseline[]>([])
const driftLoading = ref(false)
const activeDriftId = ref<number | null>(null)

const benchDriftBaselines = computed(() =>
  allServerDrift.value.filter((b) => b.bench_id === benchId),
)

async function loadBenchDrift() {
  if (!bench.value) return
  driftLoading.value = true
  try {
    allServerDrift.value = await driftApi.list({ server_id: bench.value.server_id })
  } catch {
    // Non-fatal
  } finally {
    driftLoading.value = false
  }
}

// Skeleton tab bar — Sites/Apps/Config land in later sessions.
const tabs = [
  { key: 'overview', label: 'Overview' },
  { key: 'sites', label: 'Sites' },
  { key: 'apps', label: 'Apps' },
  { key: 'config', label: 'Config' },
]

const specs = computed(() => {
  const b = bench.value
  if (!b) return []
  return [
    { label: 'Frappe', value: `${versionChip(b.frappe_version)} (${b.frappe_version ?? 'unknown'})` },
    { label: 'Python', value: b.python_version ?? '—' },
    { label: 'Node', value: b.node_version ?? '—' },
    { label: 'Mode', value: b.is_production ? 'Production' : 'Dev' },
    { label: 'Status', value: b.status === 'active' ? 'Active' : 'Missing' },
    { label: 'Path', value: b.path },
    { label: 'Discovered', value: b.discovered_at ? `${relativeTime(b.discovered_at)} (${absoluteTime(b.discovered_at)})` : 'never' },
  ]
})

const portList = computed(() => (bench.value ? portRows(bench.value) : []))

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    bench.value = await benchesApi.get(benchId)
    void loadBenchDrift()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load this bench.'
  } finally {
    loading.value = false
  }
}

// -- Maintenance actions (session 1.10) + production setup (session 2.5) ------
type BenchAction = 'build' | 'restart' | 'migrate-all' | 'update' | 'setup-production'

interface BenchActionConfig {
  title: string
  verb: string
  message: string
  consequences: string[]
  backupNotice?: string
  variant?: 'standard' | 'destructive'
  targetName?: string
}

const benchOpen = ref(false)
const launching = ref(false)
const benchAction = ref<BenchAction>('build')

const benchConfig = computed<BenchActionConfig>(() => {
  const isProd = bench.value?.is_production ?? false
  switch (benchAction.value) {
    case 'restart':
      return {
        title: 'Restart bench',
        verb: 'Restart bench',
        message: isProd
          ? 'Restart this bench’s supervisor services.'
          : 'This is a development bench — it has no supervisor services.',
        consequences: isProd
          ? ['Restarts services via supervisorctl — expect brief downtime.']
          : [
              'The job will exit with an informative failure.',
              'Start the bench manually with bench start.',
            ],
      }
    case 'migrate-all':
      return {
        title: 'Migrate all sites',
        verb: 'Migrate all sites',
        message: 'Run bench migrate on every site on this bench, one at a time.',
        consequences: [
          'Applies pending database patches to each site.',
          'Best run during a maintenance window on production.',
        ],
      }
    case 'update':
      return {
        title: 'Update bench',
        verb: 'Update bench',
        message: 'Update this bench: git pull, dependencies, patches, build, restart.',
        consequences: [
          'Expect downtime while the update runs and services restart.',
          'Long-running — you’ll land on the live job log.',
        ],
        backupNotice: 'A db-only snapshot of every site on this bench.',
      }
    case 'setup-production':
      return {
        title: 'Set up production',
        verb: 'Set up production',
        variant: 'destructive',
        targetName: bench.value?.name,
        message:
          'Convert this dev bench to production: generate nginx + supervisor config and serve its sites under them.',
        consequences: [
          'Rewrites this server’s nginx and supervisor configuration.',
          'Sites will be served by nginx/supervisor instead of bench start.',
          'Runs with temporary elevated privileges, removed automatically when the job finishes.',
          'Long-running — you’ll land on the live job log.',
        ],
        backupNotice:
          'A full pre-backup of /etc/nginx and /etc/supervisor (for rollback) is taken first.',
      }
    default:
      return {
        title: 'Build assets',
        verb: 'Run build',
        message: 'Recompile this bench’s JS/CSS assets (bench build).',
        consequences: ['Rebuilds frontend assets; no downtime.'],
      }
  }
})

function askBench(action: BenchAction) {
  benchAction.value = action
  benchOpen.value = true
}

async function confirmBench() {
  if (launching.value) return
  launching.value = true
  try {
    const launch =
      benchAction.value === 'build'
        ? benchesApi.build
        : benchAction.value === 'restart'
          ? benchesApi.restart
          : benchAction.value === 'migrate-all'
            ? benchesApi.migrateAll
            : benchAction.value === 'setup-production'
              ? benchesApi.setupProduction
              : benchesApi.update
    const job = await launch(benchId)
    benchOpen.value = false
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this bench.'
        : error instanceof Error
          ? error.message
          : 'Could not start the action.'
    toast.error(message)
  } finally {
    launching.value = false
  }
}

onMounted(load)
</script>
