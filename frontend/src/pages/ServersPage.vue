<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Servers</h1>
        <p class="text-meta text-ink-2">Managed hosts reached over SSH.</p>
      </div>
      <Button v-if="canManage" variant="solid" theme="gray" label="Add server" @click="sheetOpen = true">
        <template #prefix><LucidePlus class="h-4 w-4" /></template>
      </Button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>
      <DataTable
        :columns="columns"
        :rows="servers"
        row-key="id"
        :loading="loading"
        filter-placeholder="Filter servers"
        empty-title="No servers yet"
        empty-message="Register your first Ubuntu host to start managing benches."
        height="calc(100vh - 220px)"
      >
        <template #cell-status="{ row }">
          <span class="flex items-center gap-2">
            <StatusDot :status="statusDot((row as Server).status)" />
            <span class="text-ink-2">{{ STATUS_LABEL[(row as Server).status] }}</span>
          </span>
        </template>
        <template #cell-name="{ row }">
          <button type="button" class="fdm-focus rounded font-medium text-ink-1 hover:underline" @click="open(row as Server)">
            {{ (row as Server).name }}
          </button>
        </template>
        <template #cell-env_tag="{ row }">
          <EnvironmentBadge :env="(row as Server).env_tag" />
        </template>
        <template #cell-os_version="{ row }">
          <span class="text-ink-2">{{ (row as Server).os_version ?? '—' }}</span>
        </template>
        <template #cell-last_seen="{ row }">
          <span class="text-ink-2" :title="absoluteTime((row as Server).last_seen)">
            {{ relativeTime((row as Server).last_seen) }}
          </span>
        </template>
      </DataTable>
    </div>

    <AddServerSheet :open="sheetOpen" @close="sheetOpen = false" @created="onCreated" @view="goToDetail" />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import LucidePlus from '~icons/lucide/plus'
import { serversApi, type Server } from '../api/servers'
import AddServerSheet from '../components/AddServerSheet.vue'
import DataTable from '../components/DataTable.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import type { DataTableColumn } from '../components/types'
import { STATUS_LABEL, absoluteTime, relativeTime, statusDot } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const canManage = auth.hasPermission('server:manage')

const servers = ref<Server[]>([])
const loading = ref(true)
const loadError = ref('')
const sheetOpen = ref(false)

const columns: DataTableColumn<Server>[] = [
  { key: 'status', label: 'Status', width: '140px' },
  { key: 'name', label: 'Name', sortable: true },
  { key: 'env_tag', label: 'Env', width: '90px' },
  { key: 'hostname', label: 'IP / Host', sortable: true },
  { key: 'os_version', label: 'OS' },
  { key: 'last_seen', label: 'Last seen', width: '130px' },
]

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    servers.value = await serversApi.list()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load servers.'
  } finally {
    loading.value = false
  }
}

function open(server: Server) {
  router.push(`/servers/${server.id}`)
}

function goToDetail(id: number) {
  router.push(`/servers/${id}`)
}

function onCreated() {
  load()
}

onMounted(load)
</script>
