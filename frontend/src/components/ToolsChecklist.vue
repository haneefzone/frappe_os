<template>
  <div>
    <!-- Header row: scan button + last-scanned timestamp -->
    <div class="mb-4 flex items-center justify-between">
      <p v-if="data?.last_scanned_at" class="text-meta text-ink-3" :title="absoluteTime(data.last_scanned_at)">
        Last scanned {{ relativeTime(data.last_scanned_at) }}
        <span v-if="data.frappe_major" class="ml-1">(Frappe {{ data.frappe_major }})</span>
      </p>
      <p v-else class="text-meta text-ink-3">Never scanned</p>
      <Button
        v-if="canScan"
        variant="subtle"
        theme="gray"
        :label="scanning ? 'Scanning…' : 'Scan now'"
        :loading="scanning"
        @click="triggerScan"
      >
        <template #prefix><LucideRefreshCw class="h-4 w-4" /></template>
      </Button>
    </div>

    <!-- Loading skeleton -->
    <div v-if="loading" class="space-y-4">
      <div
        v-for="g in 3"
        :key="g"
        class="overflow-hidden rounded-lg border border-line bg-surface"
      >
        <div class="border-b border-line px-4 py-2.5">
          <div class="h-3.5 w-32 animate-pulse rounded bg-raised" />
        </div>
        <div class="divide-y divide-line">
          <div
            v-for="r in 3"
            :key="r"
            class="flex items-center justify-between px-4 py-2.5"
          >
            <div class="h-3 w-40 animate-pulse rounded bg-raised" />
            <div class="h-3 w-24 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>
    </div>

    <p v-else-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>

    <!-- Never-scanned empty state (data loaded but last_scanned_at is null) -->
    <EmptyState
      v-else-if="data && !data.last_scanned_at"
      :icon="LucidePackageSearch"
      title="No scan yet"
      message="Run a scan to detect which stack tools are installed on this server and whether they match the recommended versions."
      :cta-label="canScan ? 'Scan now' : undefined"
      @cta="triggerScan"
    />

    <!-- Grouped checklist -->
    <div v-else-if="data" class="space-y-4">
      <section
        v-for="group in data.groups"
        :key="group.group"
        class="overflow-hidden rounded-lg border border-line bg-surface"
        :aria-label="group.group"
      >
        <!-- Group header: name + "n of m current" -->
        <div class="flex items-center justify-between border-b border-line px-4 py-2.5">
          <h3 class="text-label font-semibold capitalize text-ink-1">{{ group.group }}</h3>
          <span
            class="text-meta"
            :class="group.ok_count === group.total_count ? 'text-ok' : 'text-ink-3'"
          >
            {{ group.ok_count }} of {{ group.total_count }} current
          </span>
        </div>

        <table class="w-full text-left">
          <tbody class="divide-y divide-line">
            <tr v-for="tool in group.tools" :key="tool.tool_id" class="text-label">
              <!-- Name + status dot -->
              <td class="px-4 py-2.5">
                <span class="flex items-center gap-2">
                  <StatusDot :status="toolStatusDot(tool.status)" />
                  <span class="font-medium text-ink-1">{{ tool.display_name }}</span>
                  <span
                    v-if="tool.critical"
                    class="rounded px-1 py-0.5 text-meta font-medium bg-err/10 text-err"
                  >critical</span>
                </span>
                <p v-if="tool.note && tool.status !== 'ok'" class="mt-0.5 pl-4 text-meta text-ink-3">
                  {{ tool.note }}
                </p>
              </td>

              <!-- Detected version chip -->
              <td class="px-4 py-2.5">
                <span
                  v-if="tool.detected_version"
                  class="inline-block rounded px-1.5 py-0.5 font-mono text-meta"
                  :class="versionChipClass(tool.status)"
                >
                  {{ tool.detected_version }}
                </span>
                <span v-else class="text-meta text-ink-3">—</span>
              </td>

              <!-- Recommended version chip -->
              <td class="px-4 py-2.5">
                <span
                  v-if="tool.recommended_version"
                  class="inline-block rounded bg-raised px-1.5 py-0.5 font-mono text-meta text-ink-2"
                >
                  {{ tool.recommended_version }}
                </span>
                <span v-else class="text-meta text-ink-3">—</span>
              </td>

              <!-- Action button -->
              <td class="px-4 py-2.5 text-right">
                <Button
                  v-if="tool.installable && tool.status !== 'ok' && canInstall"
                  variant="subtle"
                  theme="gray"
                  size="sm"
                  :label="tool.status === 'missing' ? 'Install' : 'Upgrade'"
                  :loading="installing === tool.tool_id"
                  :disabled="installing !== null"
                  @click="askInstall(tool)"
                />
                <span v-else-if="!tool.installable && tool.status !== 'ok'" class="text-meta text-ink-3">
                  manual
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <!-- Install confirm modal -->
    <ConfirmModal
      v-model="confirmOpen"
      :title="`${pendingVerb} ${pendingTool?.display_name}`"
      :message="pendingTool?.needs_root ? 'Requires a sudoers-allowlisted root command.' : ''"
      :verb="pendingVerb"
      :loading="installing !== null"
      :consequences="installConsequences"
      @confirm="confirmInstall"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import LucidePackageSearch from '~icons/lucide/package-search'
import LucideRefreshCw from '~icons/lucide/refresh-cw'
import { ApiError } from '../api/client'
import { toolsApi, type ServerToolsOut, type ToolOut } from '../api/tools'
import { useJobsStore } from '../stores/jobs'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'
import type { Status } from './types'
import ConfirmModal from './ConfirmModal.vue'
import EmptyState from './EmptyState.vue'
import StatusDot from './StatusDot.vue'
import { toast } from './toast'

const props = defineProps<{ serverId: number }>()

const router = useRouter()
const auth = useAuthStore()
const jobsStore = useJobsStore()

const canScan = auth.hasPermission('tool:scan')
const canInstall = auth.hasPermission('server:manage')

const data = ref<ServerToolsOut | null>(null)
const loading = ref(true)
const loadError = ref('')
const scanning = ref(false)
const installing = ref<string | null>(null)

const confirmOpen = ref(false)
const pendingTool = ref<ToolOut | null>(null)

const pendingVerb = computed(() =>
  pendingTool.value?.status === 'missing' ? 'Install' : 'Upgrade',
)

const installConsequences = computed(() => {
  const tool = pendingTool.value
  if (!tool) return []
  const lines = [`Run the install job for ${tool.display_name} on the server`]
  if (tool.needs_root) lines.push('Requires the sudoers allowlist entry to be present')
  return lines
})

function toolStatusDot(status: string): Status {
  if (status === 'ok') return 'ok'
  if (status === 'outdated') return 'warn'
  if (status === 'missing') return 'err'
  return 'muted'
}

function versionChipClass(status: string): string {
  if (status === 'ok') return 'bg-ok/10 text-ok'
  if (status === 'outdated') return 'bg-warn/10 text-warn'
  if (status === 'missing') return 'bg-err/10 text-err'
  return 'bg-raised text-ink-2'
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await toolsApi.list(props.serverId)
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load tools.'
  } finally {
    loading.value = false
  }
}

async function triggerScan() {
  if (scanning.value) return
  scanning.value = true
  try {
    const job = await toolsApi.scan(props.serverId)
    jobsStore.merge(job)
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A tool job is already running on this server.'
        : error instanceof Error
          ? error.message
          : 'Could not start the scan.'
    toast.error(message)
  } finally {
    scanning.value = false
  }
}

function askInstall(tool: ToolOut) {
  pendingTool.value = tool
  confirmOpen.value = true
}

async function confirmInstall() {
  const tool = pendingTool.value
  if (!tool || installing.value) return
  installing.value = tool.tool_id
  try {
    const job = await toolsApi.install(props.serverId, tool.tool_id)
    jobsStore.merge(job)
    confirmOpen.value = false
    pendingTool.value = null
    router.push(`/jobs/${job.id}`)
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 409
        ? 'A tool job is already running on this server.'
        : error instanceof ApiError && error.status === 422
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Could not start the install.'
    toast.error(message)
  } finally {
    installing.value = null
  }
}

watch(() => props.serverId, load)
onMounted(load)
</script>
