<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center gap-3 border-b border-line px-8 py-5">
      <button
        type="button"
        class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
        aria-label="Back to sites"
        @click="router.push('/sites')"
      >
        <LucideArrowLeft class="h-4 w-4" />
      </button>
      <StatusDot v-if="site" :status="siteDot(site.status)" />
      <h1 class="truncate text-lg font-semibold text-ink-1">{{ site?.name ?? 'Site' }}</h1>
      <EnvironmentBadge v-if="site" :env="site.server_env_tag" />
      <StatusBadge
        v-if="site?.maintenance_mode"
        status="warn"
        label="Maintenance"
      />
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>

      <div v-else-if="loading" class="max-w-3xl rounded-lg border border-line bg-surface">
        <div class="border-b border-line px-4 py-2.5"><div class="h-3.5 w-24 animate-pulse rounded bg-raised" /></div>
        <div class="divide-y divide-line">
          <div v-for="i in 6" :key="i" class="flex items-center justify-between px-4 py-2">
            <div class="h-3 w-28 animate-pulse rounded bg-raised" />
            <div class="h-3 w-40 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>

      <div v-else-if="site" class="grid max-w-4xl gap-6 lg:grid-cols-2">
        <!-- Overview -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Overview</h2>
          <dl class="divide-y divide-line">
            <div v-for="spec in specs" :key="spec.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ spec.label }}</dt>
              <dd class="max-w-[62%] truncate text-label text-ink-1" :title="spec.value">{{ spec.value }}</dd>
            </div>
          </dl>
        </section>

        <!-- Quick actions -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Quick actions</h2>
          <div class="space-y-4 p-4">
            <!-- Open site -->
            <div>
              <a
                v-if="site.url"
                :href="site.url"
                target="_blank"
                rel="noopener"
                class="fdm-focus inline-flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-label font-medium text-ink-1 transition hover:border-line-strong hover:bg-raised"
              >
                Open site
                <LucideExternalLink class="h-3.5 w-3.5" />
              </a>
              <span v-else class="text-label text-ink-3">Open site — port unknown (run a discovery).</span>
              <p class="mt-1 text-meta text-ink-3">
                Opens <span class="font-mono">{{ site.url ?? '—' }}</span>. The dev server matches on
                the <strong>Host header</strong> ({{ site.name }}); if it 404s, add
                <code class="font-mono">{{ site.name }}</code> to your hosts file or send the header.
              </p>
            </div>

            <!-- Scheduler toggle -->
            <div class="flex items-center justify-between gap-3 border-t border-line pt-4">
              <div>
                <p class="text-label font-medium text-ink-1">Scheduler</p>
                <p class="text-meta text-ink-3">{{ schedulerLabel(site.scheduler_enabled) }}</p>
              </div>
              <Button
                v-if="canOperate"
                variant="subtle"
                theme="gray"
                :label="site.scheduler_enabled ? 'Disable' : 'Enable'"
                :loading="busy === 'scheduler'"
                :disabled="!!busy"
                @click="toggleScheduler"
              />
            </div>

            <!-- Maintenance toggle -->
            <div class="flex items-center justify-between gap-3 border-t border-line pt-4">
              <div>
                <p class="text-label font-medium text-ink-1">Maintenance mode</p>
                <p class="text-meta text-ink-3">{{ site.maintenance_mode ? 'On — the site is offline to users' : 'Off' }}</p>
              </div>
              <Button
                v-if="canOperate"
                :variant="site.maintenance_mode ? 'subtle' : 'solid'"
                theme="gray"
                :label="site.maintenance_mode ? 'Turn off' : 'Turn on'"
                :loading="busy === 'maintenance'"
                :disabled="!!busy"
                @click="toggleMaintenance"
              />
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import LucideExternalLink from '~icons/lucide/external-link'
import { ApiError } from '../api/client'
import { jobsApi } from '../api/jobs'
import { sitesApi, type Site } from '../api/sites'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import {
  HEALTH_LABEL as healthLabel,
  schedulerLabel,
  siteStatusDot as siteDot,
} from '../lib/sites'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const siteId = Number(route.params.id)
const auth = useAuthStore()
const canOperate = auth.hasPermission('site:operate')

const site = ref<Site | null>(null)
const loading = ref(true)
const loadError = ref('')
const busy = ref<'' | 'scheduler' | 'maintenance'>('')

const specs = computed(() => {
  const s = site.value
  if (!s) return []
  return [
    { label: 'Bench', value: s.bench_name },
    { label: 'Server', value: `${s.server_name} (${s.server_hostname})` },
    { label: 'Scheduler', value: schedulerLabel(s.scheduler_enabled) },
    { label: 'Maintenance', value: s.maintenance_mode ? 'On' : 'Off' },
    { label: 'Health', value: healthLabel[s.health] },
    { label: 'Status', value: s.status === 'active' ? 'Active' : 'Missing' },
    { label: 'Web port', value: s.webserver_port != null ? String(s.webserver_port) : '—' },
    { label: 'Discovered', value: s.discovered_at ? `${relativeTime(s.discovered_at)} (${absoluteTime(s.discovered_at)})` : 'never' },
  ]
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    site.value = await sitesApi.get(siteId)
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load this site.'
  } finally {
    loading.value = false
  }
}

const TERMINAL = new Set(['success', 'failure', 'cancelled'])

async function runToggle(kind: 'scheduler' | 'maintenance', launch: () => Promise<{ id: number }>) {
  if (busy.value) return
  busy.value = kind
  try {
    const job = await launch()
    let status = 'pending'
    for (let i = 0; i < 40 && !TERMINAL.has(status); i++) {
      await new Promise((r) => setTimeout(r, 800))
      status = (await jobsApi.get(job.id)).status
    }
    site.value = await sitesApi.get(siteId)
    if (status === 'success') toast.success(`${kind === 'scheduler' ? 'Scheduler' : 'Maintenance'} updated.`)
    else if (TERMINAL.has(status)) toast.error(`Job ended: ${status}.`)
    else toast.info('Still running — refresh shortly.')
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A job is already running on this site.'
        : error instanceof Error
          ? error.message
          : 'Could not run the action.'
    toast.error(message)
  } finally {
    busy.value = ''
  }
}

function toggleScheduler() {
  if (!site.value) return
  const enable = !site.value.scheduler_enabled
  runToggle('scheduler', () => sitesApi.setScheduler(siteId, enable))
}

function toggleMaintenance() {
  if (!site.value) return
  const enable = !site.value.maintenance_mode
  runToggle('maintenance', () => sitesApi.setMaintenance(siteId, enable))
}

onMounted(load)
</script>
