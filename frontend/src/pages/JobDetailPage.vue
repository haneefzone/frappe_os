<template>
  <div class="flex h-full flex-col">
    <!-- Header -->
    <header class="border-b border-line px-8 py-5">
      <div class="flex items-start justify-between gap-4">
        <div class="flex min-w-0 items-start gap-3">
          <button
            type="button"
            class="fdm-focus mt-0.5 rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
            aria-label="Back to jobs"
            @click="router.push('/jobs')"
          >
            <LucideArrowLeft class="h-4 w-4" />
          </button>
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <h1 class="truncate font-mono text-lg font-semibold text-ink-1">
                {{ job?.action_name ?? 'Job' }}
              </h1>
              <StatusBadge
                v-if="job"
                :status="jobStatusDot(job.status)"
                :label="JOB_STATUS_LABEL[job.status]"
              />
              <span v-if="job?.exit_code != null" class="text-meta text-ink-3">
                exit {{ job.exit_code }}
              </span>
            </div>
            <p v-if="job" class="mt-1 text-meta text-ink-3">
              {{ targetLabel }} · priority {{ job.priority }} · started
              <span :title="absoluteTime(job.started_at ?? job.created_at)">{{ relativeTime(job.started_at ?? job.created_at) }}</span>
              <span v-if="job.retry_count"> · {{ job.retry_count }} auto-retries</span>
            </p>
          </div>
        </div>

        <div v-if="job && canManage" class="flex shrink-0 items-center gap-2">
          <Button
            v-if="!isTerminal(job.status)"
            variant="subtle"
            theme="gray"
            :label="acting ? 'Cancelling…' : 'Cancel'"
            :loading="acting"
            @click="cancel"
          >
            <template #prefix><LucideBan class="h-4 w-4" /></template>
          </Button>
          <Button
            v-else-if="job.status === 'failure' || job.status === 'cancelled'"
            variant="subtle"
            theme="gray"
            :label="acting ? 'Retrying…' : 'Retry'"
            :loading="acting"
            @click="retry"
          >
            <template #prefix><LucideRotateCcw class="h-4 w-4" /></template>
          </Button>
        </div>
      </div>

      <!-- Sanitized params + exact command -->
      <div v-if="job" class="mt-3 flex flex-col gap-2">
        <div v-if="paramEntries.length" class="flex flex-wrap items-center gap-1.5">
          <span
            v-for="[key, value] in paramEntries"
            :key="key"
            class="rounded border border-line bg-raised px-2 py-0.5 font-mono text-meta text-ink-2"
          >
            {{ key }}=<span class="text-ink-1">{{ value }}</span>
          </span>
        </div>
        <div>
          <button
            type="button"
            class="fdm-focus flex items-center gap-1 rounded text-meta text-ink-2 transition hover:text-ink-1"
            @click="toggleCommand"
          >
            <LucideChevronRight class="h-3 w-3 transition-transform" :class="{ 'rotate-90': showCommand }" />
            Show exact command
          </button>
          <pre
            v-if="showCommand"
            class="mt-1.5 overflow-x-auto rounded-lg border border-line bg-base px-3 py-2 font-mono text-meta leading-relaxed text-ink-1"
          >{{ commandText || 'Loading…' }}</pre>
        </div>
      </div>
    </header>

    <!-- Body: timeline left, live logs right -->
    <div class="min-h-0 flex-1 overflow-hidden p-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>
      <p v-else-if="actionError" class="mb-3 text-label text-err" role="alert">{{ actionError }}</p>

      <div v-if="job" class="grid h-full min-h-0 gap-6 lg:grid-cols-[minmax(280px,380px)_1fr]">
        <section class="flex min-h-0 flex-col gap-6 overflow-y-auto">
          <!-- Ask AI to analyze (uiux-spec §8/§17) — only for failed jobs -->
          <div
            v-if="job.status === 'failure' && (canManage || analysis)"
            class="rounded-lg border border-line bg-surface p-5"
          >
            <div class="flex items-start justify-between gap-3">
              <div>
                <h2 class="flex items-center gap-1.5 text-label font-semibold text-ink-1">
                  <LucideSparkles class="h-4 w-4 text-run" />
                  AI analysis
                </h2>
                <p class="mt-1 text-meta text-ink-3">
                  Send the sanitized log and job context to Claude for a root-cause read.
                </p>
              </div>
              <Button
                v-if="canManage && !analysisRunning && (!analysis || analysis.status === 'failure')"
                variant="subtle"
                theme="gray"
                :label="analysis ? 'Re-analyze' : 'Ask AI to analyze'"
                @click="analyze"
              >
                <template #prefix><LucideSparkles class="h-4 w-4" /></template>
              </Button>
            </div>

            <p v-if="analysisError" class="mt-3 text-label text-err" role="alert">
              {{ analysisError }}
            </p>

            <div
              v-if="analysisRunning"
              class="mt-4 flex items-center gap-2 text-label text-ink-2"
              aria-live="polite"
            >
              <LucideLoader2 class="h-4 w-4 animate-spin text-run" />
              Analyzing…
            </div>

            <div v-else-if="analysis && analysis.status === 'success'" class="mt-4 flex flex-col gap-4">
              <div v-if="analysis.root_cause">
                <h3 class="text-meta font-semibold uppercase tracking-wide text-ink-3">Root cause</h3>
                <p class="mt-1 whitespace-pre-wrap text-label leading-relaxed text-ink-1">
                  {{ analysis.root_cause }}
                </p>
              </div>
              <div v-if="analysis.suggested_fix">
                <h3 class="text-meta font-semibold uppercase tracking-wide text-ink-3">Suggested fix</h3>
                <p class="mt-1 whitespace-pre-wrap text-label leading-relaxed text-ink-1">
                  {{ analysis.suggested_fix }}
                </p>
              </div>
              <div v-if="analysis.summary && !analysis.root_cause && !analysis.suggested_fix">
                <p class="whitespace-pre-wrap text-label leading-relaxed text-ink-1">
                  {{ analysis.summary }}
                </p>
              </div>
              <p v-if="analysis.model" class="text-meta text-ink-3">
                Analyzed with {{ analysis.model }}
              </p>
            </div>

            <p
              v-else-if="analysis && analysis.status === 'failure'"
              class="mt-3 text-label text-err"
              role="alert"
            >
              {{ analysis.error || 'The analysis could not be completed.' }}
            </p>
          </div>

          <div class="min-h-0 rounded-lg border border-line bg-surface p-5">
            <h2 class="mb-4 text-label font-semibold text-ink-1">Steps</h2>
            <JobTimeline v-if="timelineSteps.length" :steps="timelineSteps" auto-expand-failed />
            <p v-else class="text-meta text-ink-3">Waiting for the first step…</p>
          </div>
        </section>

        <section class="min-h-0">
          <LogViewer
            :lines="logLines"
            title="Live logs"
            :filename="`job-${jobId}.log`"
            height="100%"
            :initial-follow="true"
          />
        </section>
      </div>
      <div v-else-if="loading" class="grid h-full min-h-0 gap-6 lg:grid-cols-[minmax(280px,380px)_1fr]">
        <div class="rounded-lg border border-line bg-surface p-5">
          <div class="mb-4 h-4 w-16 animate-pulse rounded bg-raised" />
          <div class="space-y-4">
            <div v-for="i in 4" :key="i" class="flex gap-3">
              <div class="h-4 w-4 flex-none animate-pulse rounded-full bg-raised" />
              <div class="h-4 flex-1 animate-pulse rounded bg-raised" />
            </div>
          </div>
        </div>
        <div class="rounded-lg border border-line bg-surface p-5">
          <div class="mb-3 h-4 w-20 animate-pulse rounded bg-raised" />
          <div class="space-y-2">
            <div v-for="i in 8" :key="i" class="h-3 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import LucideBan from '~icons/lucide/ban'
import LucideChevronRight from '~icons/lucide/chevron-right'
import LucideLoader2 from '~icons/lucide/loader-2'
import LucideRotateCcw from '~icons/lucide/rotate-ccw'
import LucideSparkles from '~icons/lucide/sparkles'
import { ApiError } from '../api/client'
import { type JobAnalysis, copilotApi } from '../api/copilot'
import { type JobDetail, jobsApi, streamJobLogs } from '../api/jobs'
import JobTimeline from '../components/JobTimeline.vue'
import LogViewer from '../components/LogViewer.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { JOB_STATUS_LABEL, isTerminal, jobStatusDot, toTimelineSteps } from '../lib/jobs'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const canManage = auth.hasPermission('job:manage')

const jobId = Number(route.params.id)
const job = ref<JobDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const actionError = ref('')
const acting = ref(false)

// --- header derived state -------------------------------------------------- //
const targetLabel = computed(() =>
  job.value?.target_id ? `${job.value.target_type}: ${job.value.target_id}` : job.value?.target_type,
)
const paramEntries = computed(() => Object.entries(job.value?.params_sanitized ?? {}))
const timelineSteps = computed(() => toTimelineSteps(job.value?.steps ?? []))

// --- "show exact command" (lazy) ------------------------------------------ //
const showCommand = ref(false)
const commandText = ref('')
async function toggleCommand() {
  showCommand.value = !showCommand.value
  if (showCommand.value && !commandText.value) {
    try {
      commandText.value = (await jobsApi.command(jobId)).command
    } catch {
      commandText.value = 'Command unavailable.'
    }
  }
}

// --- live logs ------------------------------------------------------------- //
const logLines = ref<string[]>([])
let lastSeq = 0
let abort: AbortController | null = null
let stopped = false

async function connectLogs() {
  // Resume from the highest seq we've rendered; on a fresh mount that's 0, so
  // the server replays the whole history before live-tailing (gap-free refresh).
  while (!stopped) {
    abort = new AbortController()
    try {
      await streamJobLogs(
        jobId,
        lastSeq,
        {
          onLog: (frame) => {
            if (frame.seq <= lastSeq) return
            lastSeq = frame.seq
            logLines.value.push(frame.content)
          },
          onEnd: () => {
            stopped = true
          },
        },
        abort.signal,
      )
    } catch {
      // Failed to (re)start — stop retrying if the job is already finished.
      if (job.value && isTerminal(job.value.status)) stopped = true
    }
    if (stopped) break
    // Unexpected close on a live job: back off briefly, then resume from lastSeq.
    await new Promise((r) => setTimeout(r, 1500))
  }
}

// --- job/steps polling (status + step timeline) ---------------------------- //
let pollTimer: ReturnType<typeof setTimeout> | null = null

async function refreshJob() {
  try {
    job.value = await jobsApi.get(jobId)
    loadError.value = ''
  } catch (e) {
    if (!job.value) loadError.value = e instanceof Error ? e.message : 'Could not load this job.'
  } finally {
    loading.value = false
  }
}

function scheduleJobPoll() {
  pollTimer = setTimeout(async () => {
    await refreshJob()
    if (job.value && !isTerminal(job.value.status)) scheduleJobPoll()
    else pollTimer = null
  }, 2500)
}

async function cancel() {
  acting.value = true
  actionError.value = ''
  try {
    job.value = await jobsApi.cancel(jobId)
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Could not cancel the job.'
  } finally {
    acting.value = false
  }
}

async function retry() {
  acting.value = true
  actionError.value = ''
  try {
    const fresh = await jobsApi.retry(jobId)
    router.push(`/jobs/${fresh.id}`)
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Could not retry the job.'
  } finally {
    acting.value = false
  }
}

// --- AI analysis ("Ask AI to analyze", uiux-spec §8) ----------------------- //
const analysis = ref<JobAnalysis | null>(null)
const analysisError = ref('')
let analysisTimer: ReturnType<typeof setTimeout> | null = null

// True while a request is in flight or the latest analysis is still cooking.
const analysisRunning = computed(
  () => analysis.value?.status === 'pending' || analysis.value?.status === 'running',
)

function stopAnalysisPoll() {
  if (analysisTimer) {
    clearTimeout(analysisTimer)
    analysisTimer = null
  }
}

function pollAnalysis() {
  stopAnalysisPoll()
  analysisTimer = setTimeout(async () => {
    try {
      analysis.value = await copilotApi.getAnalysis(jobId)
    } catch {
      // Transient read error — keep the last known state and retry.
    }
    if (analysisRunning.value) pollAnalysis()
    else analysisTimer = null
  }, 2000)
}

async function analyze() {
  analysisError.value = ''
  try {
    analysis.value = await copilotApi.analyzeJob(jobId)
    if (analysisRunning.value) pollAnalysis()
  } catch (e) {
    // 409 = AI disabled/unkeyed, 422 = job not failed — surface the message.
    analysisError.value = e instanceof ApiError ? e.message : 'Could not start the analysis.'
  }
}

async function loadExistingAnalysis() {
  try {
    analysis.value = await copilotApi.getAnalysis(jobId)
    if (analysisRunning.value) pollAnalysis()
  } catch (e) {
    // 404 = no prior analysis — that's the common case, stay silent.
    if (!(e instanceof ApiError && e.status === 404)) {
      // Any other error is non-fatal; the button lets the user try again.
    }
  }
}

onMounted(async () => {
  await refreshJob()
  if (loadError.value) return
  void connectLogs()
  if (job.value && !isTerminal(job.value.status)) scheduleJobPoll()
  if (job.value?.status === 'failure') void loadExistingAnalysis()
})

onUnmounted(() => {
  stopped = true
  abort?.abort()
  if (pollTimer) clearTimeout(pollTimer)
  stopAnalysisPoll()
})

// If a cancel/other action finishes the job, ensure the poll loop restarts to
// pick up the final step states.
watch(
  () => job.value?.status,
  (status) => {
    if (status && !isTerminal(status) && pollTimer === null) scheduleJobPoll()
  },
)
</script>
