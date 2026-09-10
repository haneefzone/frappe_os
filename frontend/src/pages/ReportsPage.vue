<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Reports</h1>
        <p class="text-meta text-ink-2">
          Prebuilt fleet, backup-evidence, and activity reports with PDF/CSV export.
        </p>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto">
      <div class="flex h-full">
        <!-- Catalog sidebar -->
        <aside class="w-72 shrink-0 overflow-y-auto border-r border-line bg-surface p-4">
          <p class="mb-3 px-1 text-meta font-medium uppercase tracking-wide text-ink-3">
            Reports
          </p>

          <div v-if="catalogLoading" class="space-y-2">
            <div v-for="i in 5" :key="i" class="h-14 animate-pulse rounded-lg bg-raised" />
          </div>

          <p v-else-if="catalogError" class="px-1 text-meta text-err" role="alert">
            {{ catalogError }}
          </p>

          <nav v-else class="space-y-1">
            <button
              v-for="r in catalog"
              :key="r.id"
              type="button"
              class="w-full rounded-lg px-3 py-2.5 text-left transition"
              :class="
                selected?.id === r.id
                  ? 'border-l-2 border-run bg-raised pl-2.5 text-ink-1'
                  : 'border-l-2 border-transparent text-ink-2 hover:bg-raised hover:text-ink-1'
              "
              @click="selectReport(r)"
            >
              <span class="block text-label font-medium">{{ r.title }}</span>
              <span class="block text-meta text-ink-3">{{ permissionLabel(r.required_permission) }}</span>
            </button>
          </nav>

          <EmptyState
            v-if="!catalogLoading && !catalogError && catalog.length === 0"
            :icon="LucideChartBar"
            title="No reports available"
            message="Your role doesn't have access to any reports."
          />
        </aside>

        <!-- Main area -->
        <div class="min-h-0 flex-1 overflow-y-auto p-6">
          <!-- Welcome state: no report selected -->
          <EmptyState
            v-if="!selected"
            :icon="LucideMousePointerClick"
            title="Select a report"
            message="Choose a report from the catalog on the left to configure and run it."
          />

          <template v-else>
            <!-- Report header -->
            <div class="mb-6">
              <div class="flex items-start justify-between gap-4">
                <div>
                  <h2 class="text-section font-semibold text-ink-1">{{ selected.title }}</h2>
                  <p class="mt-1 text-body text-ink-2">{{ selected.description }}</p>
                  <span
                    v-if="selected.evidence"
                    class="mt-2 inline-flex items-center gap-1.5 rounded-full border border-line bg-raised px-2 py-0.5 text-meta text-ink-2"
                  >
                    <LucideShieldCheck class="h-3.5 w-3.5 text-ok" />
                    ISO evidence export
                  </span>
                </div>
                <Button
                  variant="subtle"
                  theme="gray"
                  label="Schedule delivery"
                  @click="openSchedule"
                >
                  <template #prefix><LucideCalendarClock class="h-4 w-4" /></template>
                </Button>
              </div>
            </div>

            <!-- Parameters + Run -->
            <div class="mb-6 rounded-lg border border-line bg-surface p-5">
              <h3 class="mb-4 text-label font-semibold text-ink-1">Generate report</h3>

              <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <!-- Dynamic report params from catalog -->
                <div v-for="p in selected.params" :key="p.name">
                  <label :for="`param-${p.name}`" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                    {{ p.label }}<span v-if="p.required" class="ml-0.5 text-err">*</span>
                  </label>
                  <!-- enum → select -->
                  <select
                    v-if="p.kind === 'enum' && p.enum"
                    :id="`param-${p.name}`"
                    v-model="runParams[p.name]"
                    v-bind="selectAttrs"
                  >
                    <option v-for="opt in p.enum" :key="opt" :value="opt">{{ opt }}</option>
                  </select>
                  <!-- int → number input (no spinner arrows) -->
                  <input
                    v-else-if="p.kind === 'int'"
                    :id="`param-${p.name}`"
                    v-model.number="runParams[p.name]"
                    type="number"
                    :min="p.min ?? undefined"
                    :max="p.max ?? undefined"
                    :placeholder="String(p.default ?? '')"
                    v-bind="numberAttrs"
                  />
                  <!-- date or other -->
                  <input
                    v-else
                    :id="`param-${p.name}`"
                    v-model="runParams[p.name]"
                    type="text"
                    v-bind="inputAttrs"
                  />
                </div>

                <!-- Format selector (always shown) -->
                <div>
                  <label for="run-format" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">
                    Format
                  </label>
                  <select id="run-format" v-model="runFormat" v-bind="selectAttrs">
                    <option value="csv">CSV</option>
                    <option value="pdf">PDF</option>
                  </select>
                </div>
              </div>

              <p v-if="runError" class="mt-3 text-meta text-err" role="alert">{{ runError }}</p>

              <div class="mt-4 flex items-center gap-3">
                <Button
                  variant="solid"
                  theme="gray"
                  :label="running ? 'Running…' : 'Run now'"
                  :loading="running"
                  @click="runReport"
                />
                <span v-if="pendingJobId" class="text-meta text-ink-2">
                  Job
                  <RouterLink :to="`/jobs/${pendingJobId}`" class="fdm-focus rounded text-run hover:underline">
                    #{{ pendingJobId }}
                  </RouterLink>
                  in progress…
                </span>
              </div>
            </div>

            <!-- Run history -->
            <div>
              <div class="mb-3 flex items-center justify-between">
                <h3 class="text-label font-semibold text-ink-1">Run history</h3>
                <Button variant="subtle" theme="gray" size="sm" label="Refresh" @click="loadRuns">
                  <template #prefix><LucideRefreshCw class="h-3.5 w-3.5" /></template>
                </Button>
              </div>

              <div v-if="runsLoading" class="overflow-hidden rounded-lg border border-line bg-surface">
                <div v-for="i in 4" :key="i" class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0">
                  <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
                  <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
                  <div class="h-3.5 w-20 animate-pulse rounded bg-raised" />
                  <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
                </div>
              </div>

              <EmptyState
                v-else-if="runs.length === 0"
                :icon="LucideFileDown"
                title="No runs yet"
                message="Generate a report to see its history here."
              />

              <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
                <table class="w-full text-left">
                  <thead>
                    <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                      <th class="px-4 py-2 font-medium">Status</th>
                      <th class="px-4 py-2 font-medium">Format</th>
                      <th class="px-4 py-2 font-medium">Rows</th>
                      <th class="px-4 py-2 font-medium">Size</th>
                      <th class="px-4 py-2 font-medium">Created</th>
                      <th class="px-4 py-2 font-medium">Job</th>
                      <th class="px-4 py-2 font-medium">Download</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-line text-label">
                    <tr v-for="run in runs" :key="run.id" class="align-middle">
                      <td class="px-4 py-2.5">
                        <StatusBadge :status="runStatus(run.status)" :label="RUN_STATUS_LABEL[run.status] ?? run.status" />
                      </td>
                      <td class="px-4 py-2.5 font-mono uppercase text-ink-2">{{ run.format }}</td>
                      <td class="px-4 py-2.5 tabular-nums text-ink-2">
                        {{ run.row_count !== null ? run.row_count.toLocaleString() : '—' }}
                      </td>
                      <td class="px-4 py-2.5 text-ink-2">{{ formatBytes(run.artifact_bytes) }}</td>
                      <td class="whitespace-nowrap px-4 py-2.5 text-ink-3" :title="absoluteTime(run.created_at)">
                        {{ relativeTime(run.created_at) }}
                      </td>
                      <td class="px-4 py-2.5">
                        <RouterLink
                          v-if="run.job_id"
                          :to="`/jobs/${run.job_id}`"
                          class="fdm-focus rounded text-run hover:underline"
                        >
                          #{{ run.job_id }}
                        </RouterLink>
                        <span v-else class="text-ink-3">—</span>
                      </td>
                      <td class="px-4 py-2.5">
                        <a
                          v-if="run.download_url"
                          :href="run.download_url"
                          class="fdm-focus inline-flex items-center gap-1 rounded-full border border-line bg-raised px-2.5 py-0.5 text-meta text-ink-1 hover:bg-surface-active"
                          download
                        >
                          <LucideDownload class="h-3.5 w-3.5" />
                          {{ run.format.toUpperCase() }}
                        </a>
                        <span v-else-if="run.error" class="text-meta text-err" :title="run.error">
                          Failed
                        </span>
                        <span v-else class="text-ink-3">—</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import LucideCalendarClock from '~icons/lucide/calendar-clock'
import LucideChartBar from '~icons/lucide/chart-bar'
import LucideDownload from '~icons/lucide/download'
import LucideFileDown from '~icons/lucide/file-down'
import LucideMousePointerClick from '~icons/lucide/mouse-pointer-click'
import LucideRefreshCw from '~icons/lucide/refresh-cw'
import LucideShieldCheck from '~icons/lucide/shield-check'
import { formatBytes, type Report, type ReportRun, reportsApi } from '../api/reports'
import { jobsApi } from '../api/jobs'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import type { Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'

const router = useRouter()

const inputAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const selectAttrs = {
  class:
    'fdm-focus w-full appearance-none rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 focus:border-line-strong',
}

const numberAttrs = {
  class:
    'fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong [appearance:textfield]',
}

// -- Catalog -----------------------------------------------------------------

const catalog = ref<Report[]>([])
const catalogLoading = ref(true)
const catalogError = ref('')
const selected = ref<Report | null>(null)

async function loadCatalog() {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    catalog.value = await reportsApi.list()
  } catch (err) {
    catalogError.value = err instanceof Error ? err.message : 'Could not load reports.'
  } finally {
    catalogLoading.value = false
  }
}

function selectReport(r: Report) {
  selected.value = r
  // Reset run form and load history for this report
  initParams(r)
  runFormat.value = 'csv'
  runError.value = ''
  pendingJobId.value = null
  void loadRuns()
}

function permissionLabel(perm: string): string {
  if (perm === 'report:sensitive') return 'Admin only'
  return 'All roles'
}

// -- Params ------------------------------------------------------------------

const runParams = ref<Record<string, unknown>>({})
const runFormat = ref<'csv' | 'pdf'>('csv')

function initParams(r: Report) {
  const defaults: Record<string, unknown> = {}
  for (const p of r.params) {
    defaults[p.name] = p.default ?? (p.kind === 'int' ? undefined : undefined)
  }
  runParams.value = defaults
}

// -- Run now -----------------------------------------------------------------

const running = ref(false)
const runError = ref('')
const pendingJobId = ref<number | null>(null)

const TERMINAL = new Set(['success', 'failure', 'cancelled'])

async function runReport() {
  if (!selected.value || running.value) return
  running.value = true
  runError.value = ''
  pendingJobId.value = null

  try {
    // Use async job path so the UI is non-blocking (rule 3 in CLAUDE.md).
    const res = await reportsApi.run(selected.value.id, {
      format: runFormat.value,
      params: runParams.value,
    })
    pendingJobId.value = res.job_id

    // Poll the job to terminal so we can refresh history when done.
    void pollAndRefresh(res.job_id)
  } catch (err) {
    runError.value = err instanceof Error ? err.message : 'Run failed.'
    running.value = false
  }
}

async function pollAndRefresh(jobId: number) {
  let status = 'pending'
  for (let i = 0; i < 120 && !TERMINAL.has(status); i++) {
    await new Promise((r) => setTimeout(r, 1500))
    try {
      status = (await jobsApi.get(jobId)).status
    } catch {
      break
    }
  }
  running.value = false
  if (status !== 'success') {
    runError.value = `Job #${jobId} ended with status "${status}". Check the job log.`
  }
  void loadRuns()
}

// -- Run history -------------------------------------------------------------

const runs = ref<ReportRun[]>([])
const runsLoading = ref(false)

async function loadRuns() {
  if (!selected.value) return
  runsLoading.value = true
  try {
    runs.value = await reportsApi.listRuns(selected.value.id)
  } catch {
    // non-fatal; table stays empty
  } finally {
    runsLoading.value = false
  }
}

function runStatus(status: string): Status {
  switch (status) {
    case 'success':
      return 'ok'
    case 'pending':
    case 'running':
      return 'running'
    case 'failed':
      return 'err'
    default:
      return 'muted'
  }
}

// -- Run status labels -------------------------------------------------------

const RUN_STATUS_LABEL: Record<string, string> = {
  success: 'Success',
  failed: 'Failed',
  pending: 'Pending',
  running: 'Running',
  cancelled: 'Cancelled',
}

// -- Schedule action ---------------------------------------------------------

function openSchedule() {
  if (!selected.value) return
  // Navigate to the existing Schedules surface with context so it can auto-open
  // the creation sheet pre-filled with this report's name (uiux-spec B4.16).
  void router.push({
    path: '/schedules',
    query: {
      report: selected.value.id,
      name: `Report delivery: ${selected.value.title}`,
    },
  })
}

// -- Init --------------------------------------------------------------------

watch(selected, () => {
  pendingJobId.value = null
})

onMounted(loadCatalog)
</script>
