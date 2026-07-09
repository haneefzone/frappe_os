<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between gap-4 border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Jobs</h1>
        <p class="text-meta text-ink-3">
          Live queue of remote operations — steps and logs stream as they run.
        </p>
      </div>
      <div class="flex items-center gap-3">
        <label class="flex items-center gap-2 text-label text-ink-2">
          <input
            v-model="mineOnly"
            type="checkbox"
            class="fdm-focus h-3.5 w-3.5 rounded border-line bg-base text-run"
          />
          Only mine
        </label>
        <span
          v-if="store.runningCount"
          class="flex items-center gap-1.5 text-meta text-run"
          aria-live="polite"
        >
          <LucideLoader2 class="h-3.5 w-3.5 animate-spin" />
          {{ store.runningCount }} running
        </span>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-hidden p-8">
      <p v-if="store.error" class="mb-3 text-label text-err" role="alert">{{ store.error }}</p>
      <DataTable
        :columns="columns"
        :rows="rows"
        row-key="id"
        :loading="!store.loaded"
        height="calc(100vh - 220px)"
        filter-placeholder="Filter jobs"
        empty-title="No jobs yet"
        empty-message="Launch an action from a server, bench, or site to see it here."
      >
        <template #cell-status="{ row }">
          <span class="flex items-center gap-2">
            <span class="relative flex h-2.5 w-2.5 items-center justify-center">
              <span
                v-if="row.status === 'running'"
                class="absolute inline-flex h-full w-full animate-ping rounded-full bg-run/60"
              />
              <StatusDot :status="jobStatusDot(row.status)" />
            </span>
            <span class="text-label text-ink-1">{{ JOB_STATUS_LABEL[row.status] }}</span>
          </span>
        </template>

        <template #cell-action_name="{ row }">
          <RouterLink
            :to="`/jobs/${row.id}`"
            class="fdm-focus font-mono text-label text-ink-1 underline-offset-2 hover:underline"
          >
            {{ row.action_name }}
          </RouterLink>
        </template>

        <template #cell-retry_count="{ row }">
          <span class="tabular-nums" :class="row.retry_count ? 'text-warn' : 'text-ink-3'">
            {{ row.retry_count }}
          </span>
        </template>

        <template #actions="{ row }">
          <RouterLink
            :to="`/jobs/${row.id}`"
            class="fdm-focus rounded px-2 py-1 text-meta text-ink-2 transition hover:bg-raised hover:text-ink-1"
          >
            View
          </RouterLink>
        </template>
      </DataTable>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import LucideLoader2 from '~icons/lucide/loader-2'
import type { Job } from '../api/jobs'
import DataTable from '../components/DataTable.vue'
import StatusDot from '../components/StatusDot.vue'
import type { DataTableColumn } from '../components/types'
import { JOB_STATUS_LABEL, durationBetween, jobStatusDot } from '../lib/jobs'
import { useAuthStore } from '../stores/auth'
import { useJobsStore } from '../stores/jobs'

const store = useJobsStore()
const auth = useAuthStore()
const mineOnly = ref(false)

const rows = computed<Job[]>(() => {
  const uid = auth.user?.id
  return mineOnly.value && uid != null
    ? store.jobs.filter((j) => j.created_by === uid)
    : store.jobs
})

function userLabel(job: Job): string {
  if (job.created_by == null) return 'system'
  return job.created_by === auth.user?.id ? 'You' : `User #${job.created_by}`
}

const columns: DataTableColumn<Job>[] = [
  { key: 'status', label: 'Status', width: '130px', format: (r) => JOB_STATUS_LABEL[r.status] },
  { key: 'action_name', label: 'Action', format: (r) => r.action_name },
  {
    key: 'target',
    label: 'Target',
    format: (r) => (r.target_id ? `${r.target_type}: ${r.target_id}` : r.target_type),
  },
  {
    key: 'duration',
    label: 'Duration',
    width: '110px',
    align: 'right',
    format: (r) => durationBetween(r.started_at, r.ended_at),
  },
  { key: 'user', label: 'User', width: '120px', format: userLabel },
  { key: 'retry_count', label: 'Retries', width: '90px', align: 'right' },
]

let release: (() => void) | null = null
onMounted(() => {
  release = store.use()
})
onUnmounted(() => release?.())
</script>
