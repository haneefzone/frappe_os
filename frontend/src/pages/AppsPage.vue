<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Apps</h1>
        <p class="text-meta text-ink-2">App sources and where each app is installed.</p>
      </div>
      <Button
        v-if="canManage && tab === 'sources'"
        variant="solid"
        theme="gray"
        label="Add source"
        @click="openAdd"
      >
        <template #prefix><LucidePlus class="h-4 w-4" /></template>
      </Button>
    </header>

    <!-- Tab bar -->
    <div class="border-b border-line px-8">
      <nav class="flex gap-1" role="tablist">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          role="tab"
          :aria-selected="tab === t.key"
          class="fdm-focus -mb-px rounded-t border-b-2 px-3 py-2.5 text-label font-medium transition"
          :class="tab === t.key ? 'border-ink-1 text-ink-1' : 'border-transparent text-ink-2 hover:text-ink-1'"
          @click="tab = t.key"
        >
          {{ t.label }}
        </button>
      </nav>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Loading skeleton -->
      <div v-if="loading" class="rounded-lg border border-line bg-surface">
        <div class="border-b border-line px-4 py-3"><div class="h-4 w-40 animate-pulse rounded bg-raised" /></div>
        <div class="divide-y divide-line">
          <div v-for="j in 4" :key="j" class="flex items-center gap-6 px-4 py-3">
            <div class="h-3.5 w-40 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>

      <!-- SOURCES TAB -->
      <template v-else-if="tab === 'sources'">
        <EmptyState
          v-if="sources.length === 0"
          :icon="LucidePackage"
          title="No app sources yet"
          message="Register a marketplace app or a Git repository to install apps from."
          :cta-label="canManage ? 'Add source' : undefined"
          @cta="openAdd"
        />

        <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
          <table class="w-full text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">Name</th>
                <th class="px-4 py-2 font-medium">Kind</th>
                <th class="px-4 py-2 font-medium">Repository</th>
                <th class="px-4 py-2 font-medium">Default branch</th>
                <th class="px-4 py-2 font-medium">Private</th>
                <th class="px-4 py-2 font-medium">Created</th>
                <th v-if="canManage" class="px-4 py-2 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="s in sources" :key="s.id">
                <td class="px-4 py-2.5 font-medium text-ink-1">{{ s.name }}</td>
                <td class="px-4 py-2.5">
                  <StatusBadge :status="kindDot(s.kind)" :label="kindLabel(s.kind)" />
                </td>
                <td class="max-w-[18rem] truncate px-4 py-2.5 font-mono text-meta text-ink-2" :title="s.repo_url">
                  {{ s.repo_url }}
                </td>
                <td class="px-4 py-2.5 font-mono text-ink-2">{{ s.default_branch ?? '—' }}</td>
                <td class="px-4 py-2.5">
                  <span v-if="s.is_private" class="inline-flex items-center gap-1 text-ink-2">
                    <LucideLock class="h-3.5 w-3.5" :class="s.has_deploy_key ? 'text-ok' : 'text-warn'" />
                    <span class="text-meta">{{ s.has_deploy_key ? 'Key set' : 'No key' }}</span>
                  </span>
                  <span v-else class="text-ink-3">Public</span>
                </td>
                <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(s.created_at)">
                  {{ relativeTime(s.created_at) }}
                </td>
                <td v-if="canManage" class="px-4 py-2.5">
                  <div class="flex justify-end gap-1">
                    <button
                      type="button"
                      class="fdm-focus rounded p-1.5 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                      :aria-label="`Edit ${s.name}`"
                      @click="openEdit(s)"
                    >
                      <LucidePencil class="h-4 w-4" />
                    </button>
                    <button
                      type="button"
                      class="fdm-focus rounded p-1.5 text-ink-3 transition hover:bg-raised hover:text-err"
                      :aria-label="`Delete ${s.name}`"
                      @click="askDelete(s)"
                    >
                      <LucideTrash2 class="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <!-- INSTALLED TAB (app × site matrix) -->
      <template v-else>
        <EmptyState
          v-if="installed.length === 0"
          :icon="LucideLayoutGrid"
          title="No apps installed"
          message="Install an app on a site from the site's detail page to see it here."
        />

        <div v-else>
          <p class="mb-3 text-meta text-ink-3">App × site — version chips.</p>
          <div class="overflow-x-auto rounded-lg border border-line bg-surface">
            <table class="min-w-full border-collapse text-left">
              <thead>
                <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                  <th class="sticky left-0 z-10 bg-surface px-4 py-2 font-medium">App</th>
                  <th v-for="site in matrixSites" :key="site" class="px-4 py-2 font-medium">{{ site }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-line text-label">
                <tr v-for="app in matrixApps" :key="app">
                  <td class="sticky left-0 z-10 bg-surface px-4 py-2.5 font-medium text-ink-1">{{ app }}</td>
                  <td v-for="site in matrixSites" :key="site" class="px-4 py-2.5">
                    <span
                      v-if="cell(app, site)"
                      class="inline-block rounded-full border border-line bg-raised px-2 py-0.5 font-mono text-meta text-ink-1"
                      :title="cellTitle(app, site)"
                    >
                      {{ cell(app, site) }}
                    </span>
                    <span v-else class="text-ink-3">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </div>

    <!-- Add / edit source sheet -->
    <AddSourceSheet :open="sheetOpen" :source="editingSource" @close="sheetOpen = false" @saved="onSaved" />

    <!-- Delete source confirm -->
    <ConfirmModal
      v-model="deleteOpen"
      title="Delete source"
      :message="deleteTarget ? `Remove the app source “${deleteTarget.name}”.` : ''"
      verb="Delete source"
      variant="destructive"
      :loading="deleting"
      :consequences="[
        'Removes this source from the platform.',
        'Apps already installed from it are not touched.',
      ]"
      @confirm="confirmDelete"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import LucideLayoutGrid from '~icons/lucide/layout-grid'
import LucideLock from '~icons/lucide/lock'
import LucidePackage from '~icons/lucide/package'
import LucidePencil from '~icons/lucide/pencil'
import LucidePlus from '~icons/lucide/plus'
import LucideTrash2 from '~icons/lucide/trash-2'
import { appsApi, type AppSource, type AppSourceKind, type InstalledApp } from '../api/apps'
import AddSourceSheet from '../components/AddSourceSheet.vue'
import ConfirmModal from '../components/ConfirmModal.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { toast } from '../components/toast'
import type { Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

type Tab = 'sources' | 'installed'

const tabs: { key: Tab; label: string }[] = [
  { key: 'sources', label: 'Sources' },
  { key: 'installed', label: 'Installed' },
]

const auth = useAuthStore()
const canManage = auth.hasPermission('app:manage')

const tab = ref<Tab>('sources')
const loading = ref(true)
const loadError = ref('')

const sources = ref<AppSource[]>([])
const installed = ref<InstalledApp[]>([])

// -- Source badge presentation ----------------------------------------------
function kindLabel(kind: AppSourceKind): string {
  if (kind === 'marketplace') return 'Marketplace'
  if (kind === 'github') return 'GitHub'
  if (kind === 'gitlab') return 'GitLab'
  return kind
}
function kindDot(kind: AppSourceKind): Status {
  if (kind === 'marketplace') return 'ok'
  if (kind === 'github' || kind === 'gitlab') return 'running'
  return 'muted'
}

// -- Installed matrix --------------------------------------------------------
const matrixApps = computed(() =>
  Array.from(new Set(installed.value.map((i) => i.app_name))).sort((a, b) => a.localeCompare(b)),
)
const matrixSites = computed(() =>
  Array.from(new Set(installed.value.map((i) => i.site_name))).sort((a, b) => a.localeCompare(b)),
)
function entryFor(app: string, site: string): InstalledApp | undefined {
  return installed.value.find((i) => i.app_name === app && i.site_name === site)
}
function cell(app: string, site: string): string {
  const e = entryFor(app, site)
  if (!e) return ''
  return e.version ?? e.branch ?? '✓'
}
function cellTitle(app: string, site: string): string {
  const e = entryFor(app, site)
  if (!e) return ''
  return `${app} on ${site}${e.branch ? ` · ${e.branch}` : ''}${e.version ? ` · ${e.version}` : ''}`
}

// -- Add / edit sheet --------------------------------------------------------
const sheetOpen = ref(false)
const editingSource = ref<AppSource | null>(null)

function openAdd() {
  editingSource.value = null
  sheetOpen.value = true
}
function openEdit(source: AppSource) {
  editingSource.value = source
  sheetOpen.value = true
}
function onSaved() {
  toast.success(editingSource.value ? 'Source updated.' : 'Source added.')
  load()
}

// -- Delete source -----------------------------------------------------------
const deleteOpen = ref(false)
const deleting = ref(false)
const deleteTarget = ref<AppSource | null>(null)

function askDelete(source: AppSource) {
  deleteTarget.value = source
  deleteOpen.value = true
}
async function confirmDelete() {
  if (!deleteTarget.value || deleting.value) return
  deleting.value = true
  try {
    await appsApi.deleteSource(deleteTarget.value.id)
    toast.success('Source deleted.')
    deleteOpen.value = false
    load()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not delete the source.')
  } finally {
    deleting.value = false
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [srcs, inst] = await Promise.all([appsApi.listSources(), appsApi.listInstalled()])
    sources.value = srcs
    installed.value = inst
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load apps.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
