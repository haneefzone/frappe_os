<template>
  <div class="fixed inset-0 z-40 flex flex-col bg-base">
    <!-- Header -->
    <header class="flex items-center justify-between gap-3 border-b border-line px-6 py-4">
      <div class="flex min-w-0 items-center gap-3">
        <button
          type="button"
          class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
          aria-label="Back to agents"
          @click="$emit('close')"
        >
          <LucideArrowLeft class="h-4 w-4" />
        </button>
        <LucideBot class="h-5 w-5 shrink-0 text-ink-3" />
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <h1 class="truncate text-lg font-semibold text-ink-1">
              {{ session?.agent_name ?? 'Agent session' }}
            </h1>
            <StatusBadge
              v-if="session"
              :status="sessionStatusDot(session.status)"
              :label="SESSION_STATUS_LABEL[session.status]"
            />
            <span
              v-if="session?.read_only"
              class="rounded-full border border-line bg-raised px-2 py-0.5 text-meta text-ink-2"
            >
              READ-ONLY
            </span>
          </div>
          <p v-if="session" class="mt-0.5 truncate font-mono text-meta text-ink-3">
            {{ session.ssh_username }}@{{ session.server_name ?? `server #${session.server_id}` }}
            · {{ session.working_dir }}
          </p>
        </div>
      </div>

      <div class="flex shrink-0 items-center gap-2">
        <Button
          v-if="phase === 'terminal'"
          variant="solid"
          theme="gray"
          label="End session"
          :loading="ending"
          @click="endSession"
        >
          <template #prefix><LucideSquare class="h-3.5 w-3.5" /></template>
        </Button>
      </div>
    </header>

    <p v-if="error" class="border-b border-line bg-err/5 px-6 py-2 text-label text-err" role="alert">
      {{ error }}
    </p>

    <div class="min-h-0 flex-1 overflow-hidden">
      <!-- Preparing snapshot -->
      <div
        v-if="phase === 'starting'"
        class="flex h-full flex-col items-center justify-center gap-3 text-ink-3"
      >
        <LucideLoader2 class="h-8 w-8 animate-spin text-run" />
        <p class="text-body text-ink-1">Preparing pre-change snapshot…</p>
        <p class="text-meta">The session opens as soon as the snapshot completes.</p>
      </div>

      <!-- Live terminal -->
      <div v-else-if="phase === 'terminal'" class="relative h-full">
        <TerminalTab
          v-if="termTab"
          :tab="termTab"
          :active="true"
          @disconnected="onTermDisconnected"
        />
      </div>

      <!-- Diff review -->
      <div v-else-if="phase === 'review'" class="h-full overflow-y-auto p-6">
        <div class="mx-auto max-w-4xl space-y-4">
          <div
            v-if="capturing"
            class="flex items-center gap-2 text-label text-ink-2"
          >
            <LucideLoader2 class="h-4 w-4 animate-spin text-run" />
            Capturing changes…
          </div>

          <template v-else>
            <div
              v-if="session?.read_only && session?.status === 'rolledback'"
              class="flex items-start gap-2 rounded-lg border border-warn/35 bg-warn/10 px-3 py-2.5 text-label text-warn"
            >
              <LucideInfo class="mt-0.5 h-4 w-4 shrink-0" />
              Read-only session — changes were reverted automatically.
            </div>

            <div class="flex items-center justify-between gap-3">
              <h2 class="text-section font-semibold text-ink-1">Review changes</h2>
              <RouterLink
                v-if="session?.diff_job_id"
                :to="`/jobs/${session.diff_job_id}`"
                class="fdm-focus text-meta text-ink-3 hover:text-ink-1"
              >
                View capture job →
              </RouterLink>
            </div>

            <DiffViewer :diff="session?.diff_text ?? null" />

            <!-- Final disposition -->
            <div
              v-if="session && (session.status === 'applied' || session.status === 'rolledback') && !session.read_only"
              class="flex items-center justify-between gap-3 rounded-lg border border-line bg-surface px-4 py-3"
            >
              <span class="flex items-center gap-2 text-label">
                <StatusDot :status="session.status === 'applied' ? 'ok' : 'warn'" />
                <span class="text-ink-1">
                  {{ session.status === 'applied' ? 'Changes applied.' : 'Changes rolled back.' }}
                </span>
              </span>
              <RouterLink
                v-if="session.resolve_job_id"
                :to="`/jobs/${session.resolve_job_id}`"
                class="fdm-focus text-meta text-ink-3 hover:text-ink-1"
              >
                View job →
              </RouterLink>
            </div>

            <!-- Apply / rollback actions (only while still reviewing, read-write) -->
            <div
              v-else-if="session && session.status === 'reviewing' && hasDiff"
              class="flex items-center justify-end gap-2"
            >
              <Button
                variant="subtle"
                theme="red"
                label="Roll back"
                :disabled="resolving"
                @click="confirmRollback = true"
              />
              <Button
                v-if="!session.read_only"
                variant="solid"
                theme="gray"
                label="Apply changes"
                :loading="resolving"
                @click="applyChanges"
              />
            </div>
          </template>
        </div>
      </div>
    </div>

    <ConfirmModal
      v-model="confirmRollback"
      variant="destructive"
      title="Roll back this session"
      verb="Roll back"
      :loading="resolving"
      :consequences="[
        'Discards the agent\'s changes and restores the pre-change snapshot.',
        'This cannot be undone once the snapshot is restored.',
      ]"
      @confirm="rollbackChanges"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { onBeforeUnmount, ref } from 'vue'
import { RouterLink } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import LucideBot from '~icons/lucide/bot'
import LucideInfo from '~icons/lucide/info'
import LucideLoader2 from '~icons/lucide/loader-2'
import LucideSquare from '~icons/lucide/square'
import { aiAgentsApi, wsUrl, type Session } from '../api/aiAgents'
import { ApiError } from '../api/client'
import { jobsApi, type JobDetail } from '../api/jobs'
import { SESSION_STATUS_LABEL, sessionStatusDot } from '../lib/aiAgents'
import { isTerminal } from '../lib/jobs'
import ConfirmModal from './ConfirmModal.vue'
import DiffViewer from './DiffViewer.vue'
import StatusBadge from './StatusBadge.vue'
import StatusDot from './StatusDot.vue'
import TerminalTab from './TerminalTab.vue'
import type { TerminalTabState } from '../pages/TerminalPage.vue'

type Phase = 'starting' | 'terminal' | 'review'

const props = defineProps<{ session: Session }>()
const emit = defineEmits<{ close: []; changed: [] }>()

const session = ref<Session>(props.session)
const phase = ref<Phase>(session.value.status === 'ready' ? 'terminal' : 'starting')
const termTab = ref<TerminalTabState | null>(null)
const error = ref('')

const ending = ref(false)
const capturing = ref(false)
const resolving = ref(false)
const confirmRollback = ref(false)

const hasDiff = ref(false)

let cancelled = false

function computeHasDiff() {
  hasDiff.value = !!(session.value.diff_text && session.value.diff_text.trim())
}

// --- ready polling: wait for the snapshot, then open the terminal ---------- //
async function waitUntilReady() {
  while (!cancelled) {
    if (session.value.status === 'ready') {
      await openTerminal()
      return
    }
    if (session.value.status === 'error') {
      error.value = session.value.close_reason || 'The pre-change snapshot failed.'
      return
    }
    await sleep(1500)
    if (cancelled) return
    try {
      session.value = await aiAgentsApi.getSession(session.value.id)
    } catch (e) {
      error.value = e instanceof ApiError ? e.message : 'Lost contact with the session.'
      return
    }
  }
}

async function openTerminal() {
  try {
    const t = await aiAgentsApi.terminalTicket(session.value.id)
    termTab.value = {
      id: `agent-${session.value.id}`,
      label: `${t.ssh_username}@${t.server_name}`,
      serverId: session.value.server_id,
      serverName: t.server_name,
      sshUsername: t.ssh_username,
      wsUrl: wsUrl(t.ticket),
      status: 'connecting',
      idleWarning: false,
    }
    phase.value = 'terminal'
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Could not open the agent terminal.'
  }
}

function onTermDisconnected() {
  // The WS closed (idle timeout or server end). Leave the End button available.
}

// --- ending: capture the diff, then review --------------------------------- //
async function endSession() {
  ending.value = true
  error.value = ''
  try {
    const job = await aiAgentsApi.endSession(session.value.id)
    phase.value = 'review'
    capturing.value = true
    await pollJob(job.id)
    session.value = await aiAgentsApi.getSession(session.value.id)
    computeHasDiff()
    emit('changed')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Could not end the session.'
    phase.value = 'terminal'
  } finally {
    ending.value = false
    capturing.value = false
  }
}

// --- apply / rollback ------------------------------------------------------ //
async function applyChanges() {
  resolving.value = true
  error.value = ''
  try {
    const job = await aiAgentsApi.apply(session.value.id)
    await pollJob(job.id)
    session.value = await aiAgentsApi.getSession(session.value.id)
    emit('changed')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Could not apply the changes.'
  } finally {
    resolving.value = false
  }
}

async function rollbackChanges() {
  confirmRollback.value = false
  resolving.value = true
  error.value = ''
  try {
    const job = await aiAgentsApi.rollback(session.value.id)
    await pollJob(job.id)
    session.value = await aiAgentsApi.getSession(session.value.id)
    emit('changed')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Could not roll back the changes.'
  } finally {
    resolving.value = false
  }
}

// --- helpers --------------------------------------------------------------- //
function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

async function pollJob(jobId: number): Promise<JobDetail> {
  for (;;) {
    const job = await jobsApi.get(jobId)
    if (isTerminal(job.status)) return job
    await sleep(1500)
  }
}

// Kick off the right flow on mount.
computeHasDiff()
if (session.value.status === 'ready') {
  openTerminal()
} else if (session.value.status === 'reviewing' || session.value.status === 'applied' || session.value.status === 'rolledback') {
  phase.value = 'review'
  // Ensure we have diff_text (list endpoint omits it).
  aiAgentsApi
    .getSession(session.value.id)
    .then((s) => {
      session.value = s
      computeHasDiff()
    })
    .catch(() => {})
} else {
  waitUntilReady()
}

onBeforeUnmount(() => {
  cancelled = true
})
</script>
