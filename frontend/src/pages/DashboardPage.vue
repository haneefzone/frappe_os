<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Dashboard</h1>
        <p class="text-meta text-ink-2">Fleet health, backup compliance, and running jobs at a glance.</p>
      </div>
      <span v-if="data && !loading" class="text-meta text-ink-3" :title="absoluteTime(data.generated_at)">
        Updated {{ relativeTime(data.generated_at) }}
      </span>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Loading skeleton -->
      <div v-if="loading && !data" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div v-for="i in 4" :key="i" class="rounded-lg border border-line bg-surface p-4">
          <div class="h-3 w-24 animate-pulse rounded bg-raised" />
          <div class="mt-3 h-7 w-16 animate-pulse rounded bg-raised" />
          <div class="mt-2 h-3 w-32 animate-pulse rounded bg-raised" />
        </div>
      </div>

      <!-- First-run onboarding -->
      <div v-else-if="data && !data.onboarding.has_servers" class="mx-auto max-w-2xl">
        <div class="rounded-lg border border-line bg-surface">
          <EmptyState
            :icon="LucideRocket"
            title="Welcome — let's get your fleet online"
            message="Three steps to manage your first Frappe deployment from here."
          />
          <ol class="space-y-3 px-6 pb-6">
            <li v-for="(step, i) in onboardingSteps" :key="i" class="flex items-start gap-3">
              <span
                class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-line-strong text-meta font-semibold text-ink-2"
              >
                {{ i + 1 }}
              </span>
              <div>
                <p class="text-body font-medium text-ink-1">{{ step.title }}</p>
                <p class="text-label text-ink-2">{{ step.detail }}</p>
              </div>
            </li>
          </ol>
          <div class="flex justify-center border-t border-line px-6 py-4">
            <Button variant="solid" theme="gray" label="Add a server" @click="router.push('/servers')">
              <template #prefix><LucidePlus class="h-4 w-4" /></template>
            </Button>
          </div>
        </div>
      </div>

      <!-- Populated dashboard -->
      <div v-else-if="data" class="space-y-6">
        <p v-if="data.morning_brief" class="text-body text-ink-1">{{ data.morning_brief }}</p>

        <!-- Row 1: KPIs -->
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <KPICard
            label="Fleet health"
            :value="`${Math.round(data.kpis.fleet_health_pct)}%`"
            :status="pctStatus(data.kpis.fleet_health_pct, true)"
            :sublabel="`${data.kpis.sites_up}/${data.kpis.sites_total} sites up`"
          />
          <KPICard
            label="Sites up"
            :value="`${data.kpis.sites_up}/${data.kpis.sites_total}`"
            :status="data.kpis.sites_up >= data.kpis.sites_total ? 'ok' : data.kpis.sites_up === 0 && data.kpis.sites_total > 0 ? 'err' : 'warn'"
            :sublabel="`${pctLabel(data.kpis.uptime_30d_pct)} uptime (30d)`"
          />
          <KPICard
            label="Backups 24h"
            :value="String(data.kpis.backups_24h)"
            :status="data.kpis.backups_24h > 0 ? 'ok' : 'muted'"
            :sublabel="`${Math.round(data.kpis.backup_compliance_pct)}% compliance`"
          />
          <KPICard
            label="Failed jobs 24h"
            :value="String(data.kpis.failed_jobs_24h)"
            :status="data.kpis.failed_jobs_24h > 0 ? 'err' : 'ok'"
            sublabel="Last 24 hours"
          />
        </div>

        <!-- Servers strip -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Servers</h2>
          <EmptyState
            v-if="data.servers.length === 0"
            :icon="LucideServer"
            title="No servers"
            message="Register a host to see live resource usage here."
            cta-label="Add a server"
            @cta="router.push('/servers')"
          />
          <div v-else class="grid gap-px bg-line sm:grid-cols-2 lg:grid-cols-3">
            <button
              v-for="s in data.servers"
              :key="s.id"
              type="button"
              class="fdm-focus flex flex-col gap-2 bg-surface p-4 text-left transition-colors hover:bg-raised"
              @click="router.push(`/servers/${s.id}`)"
            >
              <div class="flex items-center gap-2">
                <StatusDot :status="serverStatusDot(s.status)" />
                <span class="truncate font-medium text-ink-1">{{ s.name }}</span>
                <EnvironmentBadge :env="s.env_tag" class="ml-auto" />
              </div>
              <div class="grid gap-1.5">
                <ResourceGauge label="CPU" :pct="s.cpu_pct" />
                <ResourceGauge label="RAM" :pct="s.mem_pct" />
                <ResourceGauge label="Disk" :pct="s.disk_pct" />
              </div>
              <span class="text-meta text-ink-3" :title="absoluteTime(s.sample_ts)">
                {{ s.sample_ts ? relativeTime(s.sample_ts) : 'No sample yet' }}
              </span>
            </button>
          </div>
        </section>

        <div class="grid gap-6 lg:grid-cols-2">
          <!-- 7-day backup grid -->
          <section class="rounded-lg border border-line bg-surface">
            <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">
              Backups — last 7 days
            </h2>
            <div class="p-4">
              <div class="flex items-end gap-2">
                <div v-for="day in data.backup_grid" :key="day.date" class="flex flex-1 flex-col items-center gap-1.5">
                  <div
                    class="h-8 w-full rounded"
                    :style="{ background: backupCellColor(day) }"
                    :title="`${day.date}: ${day.success} succeeded, ${day.failed} failed`"
                  />
                  <span class="text-meta text-ink-3">{{ dayLabel(day.date) }}</span>
                </div>
              </div>
              <div class="mt-3 flex items-center gap-4 text-meta text-ink-2">
                <span class="flex items-center gap-1.5"><StatusDot status="ok" size="sm" /> Success</span>
                <span class="flex items-center gap-1.5"><StatusDot status="err" size="sm" /> Failure</span>
                <span class="flex items-center gap-1.5"><StatusDot status="muted" size="sm" /> None</span>
              </div>
            </div>
          </section>

          <!-- Running jobs feed -->
          <section class="rounded-lg border border-line bg-surface">
            <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Running jobs</h2>
            <EmptyState
              v-if="data.running_jobs.length === 0"
              :icon="LucideListChecks"
              title="Nothing running"
              message="Enqueued and in-flight jobs will appear here."
            />
            <ul v-else class="divide-y divide-line" aria-live="polite" aria-label="Running jobs">
              <li v-for="job in data.running_jobs" :key="job.id">
                <RouterLink
                  :to="`/jobs/${job.id}`"
                  class="fdm-focus flex items-center gap-3 px-4 py-2.5 transition-colors hover:bg-raised"
                >
                  <StatusDot :status="jobStatusDot(job.status)" />
                  <span class="min-w-0 flex-1 truncate text-label text-ink-1">{{ job.action_name }}</span>
                  <span v-if="job.target_id" class="truncate text-meta text-ink-3">{{ job.target_id }}</span>
                  <span class="text-meta text-ink-3" :title="absoluteTime(job.created_at)">
                    {{ relativeTime(job.created_at) }}
                  </span>
                </RouterLink>
              </li>
            </ul>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import LucideListChecks from '~icons/lucide/list-checks'
import LucidePlus from '~icons/lucide/plus'
import LucideRocket from '~icons/lucide/rocket'
import LucideServer from '~icons/lucide/server'
import { type BackupGridDay, type Dashboard, dashboardApi } from '../api/dashboard'
import EmptyState from '../components/EmptyState.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import KPICard from '../components/KPICard.vue'
import ResourceGauge from '../components/ResourceGauge.vue'
import StatusDot from '../components/StatusDot.vue'
import { STATUS_COLOR, type Status } from '../components/types'
import { absoluteTime, relativeTime, statusDot as serverStatusDot } from '../lib/servers'
import { pctLabel } from '../lib/sites'

const POLL_MS = 15000

const router = useRouter()

const data = ref<Dashboard | null>(null)
const loading = ref(true)
const loadError = ref('')
let timer: ReturnType<typeof setInterval> | null = null

const onboardingSteps = [
  { title: 'Add a server', detail: 'Register an Ubuntu host reachable over SSH.' },
  { title: 'Discover or create a bench', detail: 'Point at an existing bench or run a guided bench init.' },
  { title: 'Create a site', detail: 'Spin up your first Frappe/ERPNext site on that bench.' },
]

function pctStatus(pct: number, higherIsBetter: boolean): Status {
  const good = higherIsBetter ? pct >= 90 : pct < 70
  const bad = higherIsBetter ? pct < 50 : pct > 90
  return good ? 'ok' : bad ? 'err' : 'warn'
}

function jobStatusDot(status: string): Status {
  if (status === 'running') return 'running'
  if (status === 'failure' || status === 'cancelled') return 'err'
  if (status === 'success') return 'ok'
  return 'muted'
}

function backupCellColor(day: BackupGridDay): string {
  if (day.failed > 0) return STATUS_COLOR.err
  if (day.success > 0) return STATUS_COLOR.ok
  return 'var(--bg-raised)'
}

function dayLabel(date: string): string {
  const d = new Date(`${date}T00:00:00`)
  return d.toLocaleDateString(undefined, { weekday: 'short' }).slice(0, 2)
}

async function load(initial = false) {
  if (initial) loading.value = true
  loadError.value = ''
  try {
    data.value = await dashboardApi.get()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load the dashboard.'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void load(true)
  timer = setInterval(() => void load(), POLL_MS)
})

onUnmounted(() => {
  if (timer !== null) clearInterval(timer)
})
</script>
