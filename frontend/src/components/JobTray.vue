<template>
  <!-- Global, bottom-right. Present whenever work is in flight or the user has
       pinned it open; driven entirely by the Pinia jobs store. -->
  <div
    v-if="visible"
    class="fixed bottom-4 right-4 z-40 w-[320px] max-w-[calc(100vw-2rem)]"
  >
    <!-- Expanded: mini-list of jobs -->
    <div
      v-if="expanded"
      class="overflow-hidden rounded-lg border border-line bg-surface shadow-lg"
      role="region"
      aria-label="Jobs tray"
    >
      <div class="flex items-center justify-between border-b border-line px-3 py-2">
        <span class="flex items-center gap-2 text-label font-medium text-ink-1">
          <LucideListChecks class="h-4 w-4 text-ink-2" />
          Jobs
          <span
            v-if="runningCount"
            class="rounded-full bg-run/15 px-1.5 py-0.5 text-meta font-semibold tabular-nums text-run"
          >
            {{ runningCount }} running
          </span>
        </span>
        <button
          type="button"
          class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
          aria-label="Collapse jobs tray"
          @click="expanded = false"
        >
          <LucideChevronDown class="h-4 w-4" />
        </button>
      </div>

      <ul class="max-h-[320px] divide-y divide-line overflow-y-auto">
        <li v-for="job in trayJobs" :key="job.id">
          <button
            type="button"
            class="fdm-focus flex w-full items-center gap-2.5 px-3 py-2 text-left transition hover:bg-raised"
            @click="open(job.id)"
          >
            <LucideLoader2
              v-if="job.status === 'running'"
              class="h-3.5 w-3.5 shrink-0 animate-spin text-run"
            />
            <StatusDot v-else :status="jobStatusDot(job.status)" />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-label text-ink-1">{{ job.action_name }}</span>
              <span class="block truncate text-meta text-ink-3">
                {{ targetLabel(job) }} · {{ relativeTime(job.created_at) }}
              </span>
            </span>
            <span class="shrink-0 text-meta text-ink-2">{{ JOB_STATUS_LABEL[job.status] }}</span>
          </button>
        </li>
        <li v-if="!trayJobs.length" class="px-3 py-4 text-center text-meta text-ink-3">
          No recent jobs.
        </li>
      </ul>
      <RouterLink
        to="/jobs"
        class="fdm-focus block border-t border-line px-3 py-2 text-center text-meta text-ink-2 transition hover:bg-raised hover:text-ink-1"
        @click="expanded = false"
      >
        View all jobs
      </RouterLink>
    </div>

    <!-- Collapsed pill -->
    <button
      v-else
      type="button"
      class="fdm-focus ml-auto flex items-center gap-2 rounded-full border border-line bg-surface px-3.5 py-2 shadow-lg transition hover:border-line-strong"
      :aria-label="`${runningCount} jobs running — open jobs tray`"
      @click="expanded = true"
    >
      <LucideLoader2 v-if="runningCount" class="h-4 w-4 animate-spin text-run" />
      <LucideListChecks v-else class="h-4 w-4 text-ink-2" />
      <span class="text-label font-medium text-ink-1">
        <template v-if="runningCount">
          {{ runningCount }} {{ runningCount === 1 ? 'job' : 'jobs' }} running
        </template>
        <template v-else>Jobs</template>
      </span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import LucideChevronDown from '~icons/lucide/chevron-down'
import LucideListChecks from '~icons/lucide/list-checks'
import LucideLoader2 from '~icons/lucide/loader-2'
import type { Job } from '../api/jobs'
import { JOB_STATUS_LABEL, jobStatusDot } from '../lib/jobs'
import { relativeTime } from '../lib/servers'
import { useJobsStore } from '../stores/jobs'
import StatusDot from './StatusDot.vue'

const router = useRouter()
const store = useJobsStore()
const expanded = ref(false)

const runningCount = computed(() => store.runningCount)
// Running first, then the most recent finished ones for quick reference.
const trayJobs = computed(() => {
  const running = store.running
  const finished = store.jobs.filter((j) => !running.includes(j)).slice(0, 6)
  return [...running, ...finished].slice(0, 12)
})

// Show the tray when there is live work, or the user has expanded it manually.
const visible = computed(() => runningCount.value > 0 || (expanded.value && store.loaded))

function targetLabel(job: Job): string {
  return job.target_id ? `${job.target_type}: ${job.target_id}` : job.target_type
}

function open(id: number) {
  expanded.value = false
  router.push(`/jobs/${id}`)
}

// Keep the poll loop alive for the whole authenticated session — the tray is
// always mounted, so it is the app-wide watcher.
let release: (() => void) | null = null
onMounted(() => {
  release = store.use()
})
onUnmounted(() => release?.())
</script>
