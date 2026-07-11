<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Updates</h1>
        <p class="text-meta text-ink-2">How far behind each installed app is from its upstream.</p>
      </div>
      <div class="flex items-center gap-3">
        <span v-if="summary && !loading" class="text-meta text-ink-3">
          {{ summary.apps_behind }} of {{ summary.tracked }} tracked apps behind
        </span>
        <Button
          v-if="canManage"
          variant="solid"
          theme="gray"
          :label="refreshing ? 'Checking…' : 'Check for updates now'"
          :loading="refreshing"
          @click="checkNow"
        >
          <template #prefix><LucideRefreshCw class="h-4 w-4" /></template>
        </Button>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Summary strip -->
      <div v-if="summary" class="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KPICard
          label="Apps behind"
          :value="String(summary.apps_behind)"
          :status="summary.apps_behind > 0 ? 'warn' : 'ok'"
          :sublabel="`${summary.tracked} tracked`"
        />
        <KPICard
          label="Sites behind"
          :value="String(summary.sites_behind)"
          :status="summary.sites_behind > 0 ? 'warn' : 'ok'"
          sublabel="With app updates"
        />
        <KPICard
          label="Security updates"
          :value="String(summary.security_updates)"
          :status="summary.security_updates > 0 ? 'err' : 'ok'"
          sublabel="Flagged upstream"
        />
        <KPICard
          label="Up to date"
          :value="`${Math.round(summary.up_to_date_fraction * 100)}%`"
          :status="upToDateStatus"
          sublabel="Of tracked apps"
        />
      </div>

      <!-- Filter toggle -->
      <div class="mb-3 flex items-center justify-between">
        <label class="flex cursor-pointer items-center gap-2 text-label text-ink-1">
          <input v-model="behindOnly" type="checkbox" class="accent-white" @change="load()" />
          Show only apps with updates
        </label>
      </div>

      <!-- Empty states -->
      <EmptyState
        v-if="!loading && rows.length === 0 && !behindOnly"
        :icon="LucidePackageSearch"
        title="No apps tracked yet"
        message="Install apps from tracked sources and check for updates to see verdicts here."
      />
      <EmptyState
        v-else-if="!loading && rows.length === 0 && behindOnly"
        :icon="LucideCheckCircle2"
        title="Everything up to date"
        message="No tracked app is behind its upstream. Nice."
      />

      <DataTable
        v-else
        :columns="columns"
        :rows="rows"
        :row-key="(r) => r.installed_app_id"
        :loading="loading"
        filter-placeholder="Filter by app, site, or bench"
        empty-title="No updates"
        empty-message="Nothing to show for the current filter."
      >
        <template #cell-status="{ row }">
          <UpdateChip
            :behind-by="row.behind_by"
            :latest-ref="row.latest_ref"
            :security-update="row.security_update"
          />
        </template>

        <template #cell-installed_ref="{ row }">
          <span class="font-mono text-meta text-ink-2">{{ row.installed_ref ?? '—' }}</span>
        </template>

        <template #cell-latest_ref="{ row }">
          <span class="font-mono text-meta text-ink-2">{{ row.latest_ref ?? '—' }}</span>
        </template>

        <template #cell-checked_at="{ row }">
          <span class="inline-flex items-center gap-1.5 text-meta text-ink-3">
            <span :title="absoluteTime(row.checked_at)">{{ relativeTime(row.checked_at) }}</span>
            <LucideAlertTriangle
              v-if="row.last_error"
              class="h-3.5 w-3.5 text-err"
              :title="row.last_error"
            />
          </span>
        </template>

        <template #actions="{ row }">
          <Button variant="subtle" theme="gray" size="sm" label="View changelog" @click="openChangelog(row)" />
        </template>
      </DataTable>
    </div>

    <ChangelogSheet
      :open="sheetOpen"
      :loading="changelogLoading"
      :error="changelogError"
      :preview="changelog"
      @close="sheetOpen = false"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import LucideAlertTriangle from '~icons/lucide/alert-triangle'
import LucideCheckCircle2 from '~icons/lucide/check-circle-2'
import LucidePackageSearch from '~icons/lucide/package-search'
import LucideRefreshCw from '~icons/lucide/refresh-cw'
import { ApiError } from '../api/client'
import {
  type ChangelogPreview,
  type UpdateStatus,
  type UpdatesSummary,
  updatesApi,
} from '../api/updateAdvisor'
import ChangelogSheet from '../components/ChangelogSheet.vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'
import KPICard from '../components/KPICard.vue'
import { toast } from '../components/toast'
import type { DataTableColumn, Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canManage = auth.hasPermission('app:manage')

const rows = ref<UpdateStatus[]>([])
const summary = ref<UpdatesSummary | null>(null)
const loading = ref(true)
const loadError = ref('')
const behindOnly = ref(false)
const refreshing = ref(false)

const columns: DataTableColumn<UpdateStatus>[] = [
  { key: 'app_name', label: 'App', sortable: true },
  { key: 'site_name', label: 'Site', sortable: true },
  { key: 'bench_name', label: 'Bench', sortable: true },
  { key: 'installed_ref', label: 'Installed' },
  { key: 'latest_ref', label: 'Latest' },
  { key: 'status', label: 'Status', sortable: true, format: (r) => statusSortValue(r) },
  { key: 'checked_at', label: 'Checked', sortable: true, format: (r) => r.checked_at ?? '' },
]

function statusSortValue(r: UpdateStatus): string {
  if (r.behind_by == null) return 'zz-untracked'
  return `${String(r.behind_by).padStart(6, '0')}${r.security_update ? '-sec' : ''}`
}

const upToDateStatus = computed<Status>(() => {
  const f = summary.value?.up_to_date_fraction ?? 1
  if (f >= 0.9) return 'ok'
  if (f < 0.5) return 'err'
  return 'warn'
})

// -- Changelog drawer --------------------------------------------------------
const sheetOpen = ref(false)
const changelog = ref<ChangelogPreview | null>(null)
const changelogLoading = ref(false)
const changelogError = ref('')

async function openChangelog(row: UpdateStatus) {
  sheetOpen.value = true
  changelog.value = null
  changelogError.value = ''
  changelogLoading.value = true
  try {
    changelog.value = await updatesApi.changelog(row.installed_app_id)
  } catch (error) {
    changelogError.value = error instanceof Error ? error.message : 'Could not load the changelog.'
  } finally {
    changelogLoading.value = false
  }
}

async function checkNow() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await updatesApi.refresh()
    toast.success('Checking for updates — verdicts refresh shortly.')
    // Give the background job a moment, then reload.
    window.setTimeout(() => void load(), 2500)
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) {
      toast.error('You do not have permission to check for updates.')
    } else {
      toast.error(error instanceof Error ? error.message : 'Could not start the update check.')
    }
  } finally {
    refreshing.value = false
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [list, sum] = await Promise.all([
      updatesApi.list({ behind_only: behindOnly.value }),
      updatesApi.summary(),
    ])
    rows.value = list
    summary.value = sum
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load updates.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
