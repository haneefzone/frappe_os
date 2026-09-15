<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Apps</h1>
        <p class="text-meta text-ink-2">App sources, the install matrix, and the Frappe app store.</p>
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
          @click="switchTab(t.key)"
        >
          {{ t.label }}
        </button>
      </nav>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Loading skeleton (sources / installed) -->
      <div v-if="loading && tab !== 'store'" class="rounded-lg border border-line bg-surface">
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
      <template v-else-if="tab === 'installed'">
        <EmptyState
          v-if="installed.length === 0"
          :icon="LucideLayoutGrid"
          title="No apps installed"
          message="Install an app on a site from the site's detail page to see it here."
        />

        <div v-else>
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
                    <div v-if="cell(app, site)" class="flex flex-col items-start gap-1">
                      <span
                        class="inline-block rounded-full border border-line bg-raised px-2 py-0.5 font-mono text-meta text-ink-1"
                        :title="cellTitle(app, site)"
                      >
                        {{ cell(app, site) }}
                      </span>
                      <UpdateChip
                        v-if="entryFor(app, site)"
                        compact
                        :behind-by="entryFor(app, site)!.behind_by"
                        :latest-ref="entryFor(app, site)!.latest_ref"
                        :security-update="entryFor(app, site)!.security_update"
                      />
                    </div>
                    <span v-else class="text-ink-3">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>

      <!-- STORE TAB -->
      <template v-else>
        <!-- Site selector (catalog is site-scoped for compatibility resolution) -->
        <div class="mb-5 flex flex-wrap items-end gap-3">
          <div class="min-w-[14rem]">
            <label class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2" for="catalog-site">
              Check compatibility for site
            </label>
            <select
              id="catalog-site"
              v-model.number="catalogSiteId"
              class="fdm-focus w-full rounded-lg border border-line bg-surface px-3 py-1.5 text-label text-ink-1 focus:border-line-strong"
              aria-label="Select site for compatibility check"
              @change="onCatalogSiteChange"
            >
              <option :value="null" disabled>Select a site…</option>
              <option v-for="s in allSites" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
          </div>

          <template v-if="catalogSiteId != null">
            <div class="relative min-w-0 flex-1">
              <LucideSearch class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
              <input
                v-model="catalogSearch"
                type="search"
                placeholder="Search apps…"
                class="fdm-focus w-full rounded-lg border border-line bg-surface py-1.5 pl-9 pr-3 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
                aria-label="Search catalog apps"
              />
            </div>
            <select
              v-model="catalogCategory"
              class="fdm-focus rounded-lg border border-line bg-surface px-3 py-1.5 text-label text-ink-1 focus:border-line-strong"
              aria-label="Filter by category"
            >
              <option value="">All categories</option>
              <option v-for="cat in catalogCategories" :key="cat" :value="cat">{{ cat }}</option>
            </select>
          </template>
        </div>

        <!-- Prompt when no site selected -->
        <EmptyState
          v-if="catalogSiteId == null"
          :icon="LucidePackage"
          title="Select a site"
          message="Choose a site above to browse the app catalog with compatibility information for that bench."
        />

        <!-- Registry unavailable (503) -->
        <EmptyState
          v-else-if="catalogError503"
          :icon="LucideServerOff"
          title="App registry unavailable"
          message="The Frappe marketplace registry has not been cloned yet. Contact your platform admin."
          cta-label="Retry"
          @cta="loadCatalog"
        />

        <!-- Frappe version unknown (409) — prompt discovery run -->
        <div
          v-else-if="catalogError409"
          class="rounded-lg border border-warn/40 bg-warn/10 px-4 py-5 text-label text-warn"
          role="alert"
        >
          <p class="font-medium">Bench Frappe version unknown</p>
          <p class="mt-1">Run a discovery on this site's server to detect its Frappe version, then retry.</p>
          <Button class="mt-3" variant="subtle" theme="gray" label="Retry" @click="loadCatalog" />
        </div>

        <!-- Other error -->
        <EmptyState
          v-else-if="catalogErrorOther"
          :icon="LucideServerOff"
          title="Could not load catalog"
          :message="catalogErrorOther"
          cta-label="Retry"
          @cta="loadCatalog"
        />

        <!-- Grid skeleton -->
        <div
          v-else-if="catalogLoading"
          class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
          aria-busy="true"
          aria-label="Loading catalog"
        >
          <div v-for="i in 6" :key="i" class="rounded-lg border border-line bg-surface p-4">
            <div class="mb-3 flex items-start gap-3">
              <div class="h-10 w-10 flex-none animate-pulse rounded-lg bg-raised" />
              <div class="min-w-0 flex-1">
                <div class="mb-1.5 h-4 w-3/4 animate-pulse rounded bg-raised" />
                <div class="h-3 w-1/2 animate-pulse rounded bg-raised" />
              </div>
            </div>
            <div class="space-y-1.5">
              <div class="h-3 w-full animate-pulse rounded bg-raised" />
              <div class="h-3 w-5/6 animate-pulse rounded bg-raised" />
            </div>
          </div>
        </div>

        <!-- No results after filter -->
        <EmptyState
          v-else-if="filteredCatalog.length === 0"
          :icon="LucideSearch"
          title="No apps match"
          message="Try adjusting your search or category filter."
        />

        <!-- App cards grid -->
        <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div
            v-for="app in filteredCatalog"
            :key="app.name"
            class="flex flex-col rounded-lg border bg-surface p-4 transition"
            :class="app.is_installable ? 'border-line' : 'border-line opacity-60'"
          >
            <!-- Card header: logo + title -->
            <div class="mb-3 flex items-start gap-3">
              <div class="flex h-10 w-10 flex-none items-center justify-center overflow-hidden rounded-lg border border-line bg-raised">
                <img
                  v-if="app.logo_url"
                  :src="app.logo_url"
                  :alt="`${app.title} logo`"
                  class="h-full w-full object-contain"
                  loading="lazy"
                />
                <LucidePackage v-else class="h-5 w-5 text-ink-3" />
              </div>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="truncate font-medium text-ink-1">{{ app.title }}</span>
                  <span
                    v-if="app.installed"
                    class="flex-none rounded-full border border-ok/40 bg-ok/10 px-1.5 py-0.5 text-meta font-medium text-ok"
                  >Installed</span>
                </div>
                <div class="flex items-center gap-2 text-meta text-ink-3">
                  <span class="flex items-center gap-0.5">
                    <LucideStar class="h-3 w-3" />
                    {{ app.stars?.toLocaleString() ?? '—' }}
                  </span>
                  <span v-if="app.version">· {{ app.version }}</span>
                </div>
              </div>
            </div>

            <!-- Description -->
            <p class="mb-3 line-clamp-2 flex-1 text-label text-ink-2">{{ app.description }}</p>

            <!-- Categories -->
            <div v-if="app.categories.length" class="mb-3 flex flex-wrap gap-1">
              <span
                v-for="cat in app.categories"
                :key="cat"
                class="rounded-full border border-line bg-raised px-2 py-0.5 text-meta text-ink-2"
              >{{ cat }}</span>
            </div>

            <!-- Incompatibility banner -->
            <p
              v-if="!app.is_installable && app.incompatible_reason"
              class="mb-3 rounded-lg border border-warn/40 bg-warn/10 px-2.5 py-2 text-meta text-warn"
              role="alert"
            >
              {{ app.incompatible_reason }}
            </p>

            <!-- Footer: dependencies + install button -->
            <div class="mt-auto flex items-center justify-between gap-2">
              <span
                v-if="Object.keys(app.dependencies).length > 0"
                class="min-w-0 truncate text-meta text-ink-3"
                :title="`Requires: ${Object.keys(app.dependencies).join(', ')}`"
              >
                Requires: {{ Object.keys(app.dependencies).join(', ') }}
              </span>
              <span v-else />

              <Button
                v-if="canManage"
                :disabled="!app.is_installable"
                variant="subtle"
                theme="gray"
                size="sm"
                :label="app.installed ? 'Installed' : 'Install'"
                @click="app.is_installable && openStoreInstall(app)"
              />
            </div>
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
      :message="deleteTarget ? `Remove the app source '${deleteTarget.name}'.` : ''"
      verb="Delete source"
      variant="destructive"
      :loading="deleting"
      :consequences="[
        'Removes this source from the platform.',
        'Apps already installed from it are not touched.',
      ]"
      @confirm="confirmDelete"
    />

    <!-- Store install confirm modal -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="storeInstallOpen"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="closeStoreInstall"
        >
          <div
            role="dialog"
            aria-modal="true"
            :aria-label="`Install ${storeInstallApp?.title}`"
            class="w-full max-w-md rounded-lg border border-line bg-raised"
          >
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Install {{ storeInstallApp?.title }}</h2>
              <p class="mt-0.5 text-label text-ink-2">
                {{ storeInstallApp?.name }} · branch {{ storeInstallApp?.branch ?? 'default' }}
              </p>
            </div>

            <div class="space-y-4 px-5 py-4">
              <!-- Dependency disclosure -->
              <div
                v-if="storeInstallApp && Object.keys(storeInstallApp.dependencies).length > 0"
                class="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2.5 text-label text-warn"
                role="note"
              >
                <span class="font-medium">Requires: </span>{{ Object.keys(storeInstallApp.dependencies).join(', ') }} — these apps will also be installed.
              </div>

              <!-- Site confirmation — the site is already chosen from the catalog selector -->
              <p class="text-label text-ink-2">
                Installing on site: <span class="font-medium text-ink-1">{{ selectedSiteName }}</span>
              </p>

              <p v-if="storeInstallError" class="text-label text-err" role="alert">{{ storeInstallError }}</p>
            </div>

            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="storeInstalling" @click="closeStoreInstall" />
              <Button
                variant="solid"
                theme="gray"
                label="Install app"
                :loading="storeInstalling"
                @click="submitStoreInstall"
              />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
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
import LucideSearch from '~icons/lucide/search'
import LucideServerOff from '~icons/lucide/server-off'
import LucideStar from '~icons/lucide/star'
import LucideTrash2 from '~icons/lucide/trash-2'
import { appsApi, type AppSource, type AppSourceKind, type CatalogApp, type InstalledApp } from '../api/apps'
import { ApiError } from '../api/client'
import { sitesApi, type Site } from '../api/sites'
import AddSourceSheet from '../components/AddSourceSheet.vue'
import ConfirmModal from '../components/ConfirmModal.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import UpdateChip from '../components/UpdateChip.vue'
import { toast } from '../components/toast'
import type { Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

type Tab = 'sources' | 'installed' | 'store'

const tabs: { key: Tab; label: string }[] = [
  { key: 'sources', label: 'Sources' },
  { key: 'installed', label: 'Installed' },
  { key: 'store', label: 'Store' },
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

// -- Add / edit source sheet --------------------------------------------------------
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

// -- Catalog (store tab) -----------------------------------------------------
const allSites = ref<Site[]>([])
const catalogSiteId = ref<number | null>(null)
const catalog = ref<CatalogApp[]>([])
const catalogLoading = ref(false)
const catalogError503 = ref(false)
const catalogError409 = ref(false)
const catalogErrorOther = ref('')
const catalogSearch = ref('')
const catalogCategory = ref('')

const catalogCategories = computed(() => {
  const cats = new Set<string>()
  for (const app of catalog.value) for (const cat of app.categories) cats.add(cat)
  return Array.from(cats).sort()
})

const filteredCatalog = computed(() => {
  const q = catalogSearch.value.trim().toLowerCase()
  const cat = catalogCategory.value
  return catalog.value
    .filter((app) => {
      if (cat && !app.categories.includes(cat)) return false
      if (q && !app.name.toLowerCase().includes(q) && !app.title.toLowerCase().includes(q) && !app.description.toLowerCase().includes(q)) return false
      return true
    })
    .sort((a, b) => {
      if (a.installed !== b.installed) return a.installed ? -1 : 1
      if ((b.stars ?? 0) !== (a.stars ?? 0)) return (b.stars ?? 0) - (a.stars ?? 0)
      return a.title.localeCompare(b.title)
    })
})

async function loadCatalog() {
  if (catalogSiteId.value == null) return
  catalogLoading.value = true
  catalogError503.value = false
  catalogError409.value = false
  catalogErrorOther.value = ''
  catalog.value = []
  try {
    catalog.value = await appsApi.listCatalog(catalogSiteId.value)
  } catch (error) {
    if (error instanceof ApiError && error.status === 503) {
      catalogError503.value = true
    } else if (error instanceof ApiError && error.status === 409) {
      catalogError409.value = true
    } else {
      catalogErrorOther.value = error instanceof Error ? error.message : 'Could not load the app catalog.'
    }
  } finally {
    catalogLoading.value = false
  }
}

function onCatalogSiteChange() {
  catalog.value = []
  catalogSearch.value = ''
  catalogCategory.value = ''
  void loadCatalog()
}

async function loadAllSites() {
  try {
    allSites.value = await sitesApi.list()
    if (allSites.value.length === 1 && catalogSiteId.value == null) {
      catalogSiteId.value = allSites.value[0].id
      void loadCatalog()
    }
  } catch {
    // Non-fatal; user will see the site selector empty.
  }
}

function switchTab(key: Tab) {
  tab.value = key
  if (key === 'store' && allSites.value.length === 0) {
    void loadAllSites()
  }
}

// -- Store install confirm modal -------------------------------------------
const storeInstallOpen = ref(false)
const storeInstalling = ref(false)
const storeInstallApp = ref<CatalogApp | null>(null)
const storeInstallError = ref('')

const selectedSiteName = computed(() => {
  const s = allSites.value.find((s) => s.id === catalogSiteId.value)
  return s?.name ?? '—'
})

function openStoreInstall(app: CatalogApp) {
  storeInstallApp.value = app
  storeInstallError.value = ''
  storeInstallOpen.value = true
}

function closeStoreInstall() {
  if (!storeInstalling.value) storeInstallOpen.value = false
}

async function submitStoreInstall() {
  const app = storeInstallApp.value
  if (storeInstalling.value || catalogSiteId.value == null || !app) return
  storeInstalling.value = true
  storeInstallError.value = ''
  try {
    const job = await appsApi.install(catalogSiteId.value, {
      store_app: app.name,
    })
    storeInstallOpen.value = false
    toast.success(`Installing ${app.title} — job #${job.id} started.`)
    void load()
  } catch (error) {
    storeInstallError.value = error instanceof Error ? error.message : 'Could not start the install.'
  } finally {
    storeInstalling.value = false
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
