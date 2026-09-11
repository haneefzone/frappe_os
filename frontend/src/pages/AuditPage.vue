<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Audit Log</h1>
        <p class="text-meta text-ink-2">Immutable record of every state-changing action.</p>
      </div>
      <Button variant="subtle" theme="gray" :label="exporting ? 'Exporting…' : 'Export CSV'" :loading="exporting" @click="exportCsv">
        <template #prefix><LucideDownload class="h-4 w-4" /></template>
      </Button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <!-- Compliance report generator (FDM 4.4) — shown to users with report:generate -->
      <ComplianceReportGenerator v-if="authStore.hasPermission('report:generate')" class="mb-6" />

      <!-- Filters -->
      <div class="mb-4 flex flex-wrap items-end gap-3">
        <div>
          <label for="f-action" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">Action</label>
          <input id="f-action" v-model="filters.action" type="text" placeholder="e.g. site.create" v-bind="inputAttrs" @keyup.enter="applyFilters" />
        </div>
        <div>
          <label for="f-entity" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">Entity type</label>
          <input id="f-entity" v-model="filters.entity_type" type="text" placeholder="e.g. site" v-bind="inputAttrs" @keyup.enter="applyFilters" />
        </div>
        <div>
          <label for="f-result" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">Result</label>
          <select id="f-result" v-model="filters.result" v-bind="inputAttrs">
            <option value="">Any</option>
            <option value="ok">ok</option>
            <option value="enqueued">enqueued</option>
            <option value="denied">denied</option>
            <option value="error">error</option>
          </select>
        </div>
        <div class="min-w-[200px] flex-1">
          <label for="f-q" class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2">Search</label>
          <input id="f-q" v-model="filters.q" type="text" placeholder="Free-text search" v-bind="inputAttrs" @keyup.enter="applyFilters" />
        </div>
        <Button variant="solid" theme="gray" label="Apply filters" @click="applyFilters" />
        <Button v-if="hasFilters" variant="subtle" theme="gray" label="Clear" @click="clearFilters" />
      </div>

      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <div v-if="loading" class="overflow-hidden rounded-lg border border-line bg-surface">
        <div v-for="i in 6" :key="i" class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0">
          <div class="h-3.5 w-28 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-40 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
          <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
        </div>
      </div>

      <EmptyState
        v-else-if="entries.length === 0"
        :icon="LucideScrollText"
        title="No matching entries"
        :message="hasFilters ? 'Try widening your filters.' : 'Audited actions will appear here as the fleet is operated.'"
      />

      <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
        <table class="w-full text-left">
          <thead>
            <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
              <th class="px-4 py-2 font-medium">Time</th>
              <th class="px-4 py-2 font-medium">User</th>
              <th class="px-4 py-2 font-medium">Action</th>
              <th class="px-4 py-2 font-medium">Entity</th>
              <th class="px-4 py-2 font-medium">Result</th>
              <th class="px-4 py-2 font-medium">Source IP</th>
              <th class="px-4 py-2 font-medium">Job</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line text-label">
            <tr v-for="e in entries" :key="e.id" class="align-top">
              <td class="whitespace-nowrap px-4 py-2.5 text-ink-2" :title="absoluteTime(e.ts)">{{ relativeTime(e.ts) }}</td>
              <td class="px-4 py-2.5 text-ink-1">{{ e.user_email || '—' }}</td>
              <td class="px-4 py-2.5">
                <span class="font-mono text-ink-1">{{ e.action }}</span>
                <span v-if="e.summary" class="block text-meta text-ink-3">{{ e.summary }}</span>
              </td>
              <td class="px-4 py-2.5 text-ink-2">
                {{ e.entity_type }}<span v-if="e.entity_id" class="text-ink-3"> #{{ e.entity_id }}</span>
              </td>
              <td class="px-4 py-2.5"><StatusBadge :status="resultStatus(e.result)" :label="e.result" /></td>
              <td class="whitespace-nowrap px-4 py-2.5 font-mono text-meta text-ink-3">{{ e.source_ip || '—' }}</td>
              <td class="px-4 py-2.5">
                <RouterLink
                  v-if="e.job_id"
                  :to="`/jobs/${e.job_id}`"
                  class="fdm-focus rounded text-run hover:underline"
                >
                  #{{ e.job_id }}
                </RouterLink>
                <span v-else class="text-ink-3">—</span>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- Pagination -->
        <div class="flex items-center justify-between border-t border-line px-4 py-2 text-meta tabular-nums text-ink-2">
          <span>{{ rangeLabel }}</span>
          <div class="flex items-center gap-1">
            <Button variant="subtle" theme="gray" size="sm" label="Previous" :disabled="offset === 0" @click="prevPage" />
            <Button variant="subtle" theme="gray" size="sm" label="Next" :disabled="offset + limit >= total" @click="nextPage" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import LucideDownload from '~icons/lucide/download'
import LucideScrollText from '~icons/lucide/scroll-text'
import { type AuditEntry, type AuditFilters, auditApi } from '../api/audit'
import ComplianceReportGenerator from '../components/ComplianceReportGenerator.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { toast } from '../components/toast'
import type { Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()

const inputAttrs = {
  class:
    'fdm-focus rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong',
}

const LIMIT = 50

const filters = reactive({ action: '', entity_type: '', result: '', q: '' })
const entries = ref<AuditEntry[]>([])
const total = ref(0)
const offset = ref(0)
const limit = ref(LIMIT)
const loading = ref(true)
const loadError = ref('')
const exporting = ref(false)

const hasFilters = computed(
  () => !!(filters.action || filters.entity_type || filters.result || filters.q),
)

const rangeLabel = computed(() => {
  if (total.value === 0) return '0 entries'
  const from = offset.value + 1
  const to = Math.min(offset.value + entries.value.length, total.value)
  return `${from}–${to} of ${total.value.toLocaleString()}`
})

function currentFilters(): AuditFilters {
  return {
    action: filters.action.trim() || undefined,
    entity_type: filters.entity_type.trim() || undefined,
    result: filters.result || undefined,
    q: filters.q.trim() || undefined,
    limit: limit.value,
    offset: offset.value,
  }
}

function resultStatus(result: string): Status {
  if (result === 'ok') return 'ok'
  if (result === 'enqueued') return 'running'
  if (result === 'denied' || result === 'error') return 'err'
  return 'muted'
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await auditApi.list(currentFilters())
    entries.value = res.entries
    total.value = res.total
    limit.value = res.limit
    offset.value = res.offset
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load the audit log.'
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  offset.value = 0
  void load()
}

function clearFilters() {
  filters.action = ''
  filters.entity_type = ''
  filters.result = ''
  filters.q = ''
  offset.value = 0
  void load()
}

function nextPage() {
  if (offset.value + limit.value >= total.value) return
  offset.value += limit.value
  void load()
}

function prevPage() {
  if (offset.value === 0) return
  offset.value = Math.max(0, offset.value - limit.value)
  void load()
}

async function exportCsv() {
  if (exporting.value) return
  exporting.value = true
  try {
    // Authenticated GET → download as a blob (cookies ride along).
    const { limit: _l, offset: _o, ...rest } = currentFilters()
    void _l
    void _o
    const url = auditApi.csvUrl(rest)
    const response = await fetch(url, { credentials: 'same-origin' })
    if (!response.ok) throw new Error(`Export failed (HTTP ${response.status}).`)
    const blob = await response.blob()
    const href = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = href
    anchor.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(href)
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not export the audit log.')
  } finally {
    exporting.value = false
  }
}

onMounted(load)
</script>
