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

      <div v-else-if="server" class="grid max-w-4xl gap-6 lg:grid-cols-2">
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
      </div>
    </div>
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
import { serversApi, streamServerTest, type CheckEvent, type Server } from '../api/servers'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import type { Status } from '../components/types'
import { STATUS_LABEL, absoluteTime, relativeTime, statusDot } from '../lib/servers'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useJobsStore } from '../stores/jobs'

type RowStatus = 'pending' | 'running' | 'ok' | 'fail' | 'skipped'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const jobsStore = useJobsStore()
const canManage = auth.hasPermission('server:manage')

const serverId = Number(route.params.id)
const launching = ref(false)
const server = ref<Server | null>(null)
const loading = ref(true)
const loadError = ref('')

const TOOL_KEYS = ['git', 'python3', 'uv', 'node', 'mariadb', 'redis-server', 'wkhtmltopdf', 'bench']
const coreRows = reactive<{ key: string; label: string; status: RowStatus; value: string | null }[]>([])
const toolRows = reactive<{ key: string; label: string; status: RowStatus; value: string | null }[]>([])

const testing = ref(false)
const hasRun = ref(false)
const testError = ref('')

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

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    server.value = await serversApi.get(serverId)
    seedRows()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load this server.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
