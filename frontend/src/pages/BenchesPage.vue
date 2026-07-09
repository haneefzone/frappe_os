<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Benches</h1>
        <p class="text-meta text-ink-2">Frappe benches discovered across your servers.</p>
      </div>
      <Button
        v-if="canCreate"
        variant="solid"
        theme="gray"
        label="Create bench"
        @click="router.push('/benches/new')"
      >
        <template #prefix><LucidePlus class="h-4 w-4" /></template>
      </Button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Loading skeleton -->
      <div v-if="loading" class="space-y-6">
        <div v-for="i in 2" :key="i" class="rounded-lg border border-line bg-surface">
          <div class="border-b border-line px-4 py-3"><div class="h-4 w-40 animate-pulse rounded bg-raised" /></div>
          <div class="divide-y divide-line">
            <div v-for="j in 2" :key="j" class="flex items-center gap-6 px-4 py-3">
              <div class="h-3.5 w-32 animate-pulse rounded bg-raised" />
              <div class="h-3.5 w-12 animate-pulse rounded bg-raised" />
              <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
            </div>
          </div>
        </div>
      </div>

      <EmptyState
        v-else-if="servers.length === 0"
        title="No servers yet"
        message="Register an Ubuntu server first, then discover its benches."
      />

      <div v-else class="space-y-6">
        <section
          v-for="server in servers"
          :key="server.id"
          class="rounded-lg border border-line bg-surface"
        >
          <header class="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
            <div class="flex min-w-0 items-center gap-2">
              <StatusDot :status="serverDot(server.status)" />
              <button
                type="button"
                class="fdm-focus truncate rounded font-medium text-ink-1 hover:underline"
                @click="router.push(`/servers/${server.id}`)"
              >
                {{ server.name }}
              </button>
              <EnvironmentBadge :env="server.env_tag" />
              <span class="text-meta text-ink-3">{{ (grouped.get(server.id) ?? []).length }} bench(es)</span>
            </div>
            <Button
              v-if="canOperate"
              variant="subtle"
              theme="gray"
              :label="discovering.has(server.id) ? 'Discovering…' : 'Discover'"
              :loading="discovering.has(server.id)"
              @click="discover(server.id)"
            >
              <template #prefix><LucideRadar class="h-4 w-4" /></template>
            </Button>
          </header>

          <p
            v-if="(grouped.get(server.id) ?? []).length === 0"
            class="px-4 py-6 text-center text-meta text-ink-3"
          >
            No benches discovered on this server yet.
          </p>

          <table v-else class="w-full text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">Bench</th>
                <th class="px-4 py-2 font-medium">Version</th>
                <th class="px-4 py-2 font-medium">Python</th>
                <th class="px-4 py-2 font-medium">Node</th>
                <th class="px-4 py-2 font-medium">Mode</th>
                <th class="px-4 py-2 font-medium">Ports</th>
                <th class="px-4 py-2 font-medium">Sites</th>
                <th class="px-4 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="bench in grouped.get(server.id) ?? []" :key="bench.id">
                <td class="px-4 py-2.5">
                  <button
                    type="button"
                    class="fdm-focus rounded font-medium text-ink-1 hover:underline"
                    @click="router.push(`/benches/${bench.id}`)"
                  >
                    {{ bench.name }}
                  </button>
                  <div class="truncate font-mono text-meta text-ink-3" :title="bench.path">{{ bench.path }}</div>
                </td>
                <td class="px-4 py-2.5">
                  <!-- Neutral grey chip (design spec: version colored by status tokens only). -->
                  <span
                    class="inline-flex items-center rounded-full border border-line bg-raised px-2 py-0.5 text-meta font-medium text-ink-2"
                    :title="bench.frappe_version ?? 'unknown'"
                  >
                    {{ chip(bench.frappe_version) }}
                  </span>
                </td>
                <td class="px-4 py-2.5 font-mono text-ink-2">{{ bench.python_version ?? '—' }}</td>
                <td class="px-4 py-2.5 font-mono text-ink-2">{{ bench.node_version ?? '—' }}</td>
                <td class="px-4 py-2.5">
                  <StatusBadge
                    :status="bench.is_production ? 'running' : 'muted'"
                    :label="bench.is_production ? 'Production' : 'Dev'"
                  />
                </td>
                <td class="px-4 py-2.5">
                  <div class="relative inline-block">
                    <button
                      type="button"
                      class="fdm-focus rounded text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink-1"
                      :aria-expanded="openPorts === bench.id"
                      @click="togglePorts(bench.id)"
                    >
                      Port map
                    </button>
                    <div
                      v-if="openPorts === bench.id"
                      class="absolute left-0 top-6 z-10 w-52 rounded-lg border border-line bg-raised p-3 shadow-lg"
                    >
                      <dl class="space-y-1">
                        <div v-for="p in ports(bench)" :key="p.label" class="flex justify-between text-meta">
                          <dt class="text-ink-3">{{ p.label }}</dt>
                          <dd class="font-mono text-ink-1">{{ p.value ?? '—' }}</dd>
                        </div>
                      </dl>
                    </div>
                  </div>
                </td>
                <td class="px-4 py-2.5 text-ink-3">—</td>
                <td class="px-4 py-2.5">
                  <span class="flex items-center gap-2">
                    <StatusDot :status="benchDot(bench.status)" />
                    <span class="text-ink-2">{{ statusLabel[bench.status] }}</span>
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import LucidePlus from '~icons/lucide/plus'
import LucideRadar from '~icons/lucide/radar'
import { ApiError } from '../api/client'
import { benchesApi, type Bench } from '../api/benches'
import { jobsApi } from '../api/jobs'
import { serversApi, type Server } from '../api/servers'
import EmptyState from '../components/EmptyState.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { toast } from '../components/toast'
import {
  BENCH_STATUS_LABEL as statusLabel,
  benchStatusDot as benchDot,
  groupByServer,
  portRows as ports,
  versionChip as chip,
} from '../lib/benches'
import { statusDot as serverDot } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
// Discovery launches a bench.discover job (server:manage), the same gate the
// backend enforces; hide the button for roles that would only get a 403.
const canOperate = auth.hasPermission('server:manage')
// Guided create launches a bench.create job (bench:operate), the same gate the
// backend enforces; hide the button for roles that would only get a 403.
const canCreate = auth.hasPermission('bench:operate')

const servers = ref<Server[]>([])
const benches = ref<Bench[]>([])
const loading = ref(true)
const loadError = ref('')
const discovering = ref<Set<number>>(new Set())
const openPorts = ref<number | null>(null)

const grouped = computed(() => groupByServer(benches.value))

function togglePorts(id: number) {
  openPorts.value = openPorts.value === id ? null : id
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [srv, bnc] = await Promise.all([serversApi.list(), benchesApi.list()])
    servers.value = srv
    benches.value = bnc
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load benches.'
  } finally {
    loading.value = false
  }
}

const TERMINAL = new Set(['success', 'failure', 'cancelled'])

async function discover(serverId: number) {
  if (discovering.value.has(serverId)) return
  discovering.value = new Set(discovering.value).add(serverId)
  try {
    const job = await benchesApi.discover(serverId)
    // Poll the discovery job to completion, then refresh the inventory.
    let status = job.status
    for (let i = 0; i < 60 && !TERMINAL.has(status); i++) {
      await new Promise((r) => setTimeout(r, 1200))
      status = (await jobsApi.get(job.id)).status
    }
    benches.value = await benchesApi.list()
    if (status === 'success') toast.success('Discovery finished.')
    else if (TERMINAL.has(status)) toast.error(`Discovery ended: ${status}.`)
    else toast.info('Discovery is still running — refresh shortly.')
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A discovery job is already running on this server.'
        : error instanceof Error
          ? error.message
          : 'Could not start discovery.'
    toast.error(message)
  } finally {
    const next = new Set(discovering.value)
    next.delete(serverId)
    discovering.value = next
  }
}

onMounted(load)
</script>
