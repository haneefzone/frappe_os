<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Apps</h1>
        <p class="text-meta text-ink-2">App sources, install matrix, and the Frappe app store.</p>
      </div>
      <div class="flex items-center gap-2">
        <Button
          v-if="canManage && tab === 'store' && !catalogLoading"
          variant="subtle"
          theme="gray"
          :loading="refreshing"
          :label="refreshing ? 'Refreshing…' : 'Refresh catalog'"
          @click="refreshCatalog"
        >
          <template v-if="!refreshing" #prefix><LucideRefreshCw class="h-4 w-4" /></template>
        </Button>
        <Button
          v-if="canManage && tab === 'sources'"
          variant="solid"
          theme="gray"
          label="Add source"
          @click="openAdd"
        >
          <template #prefix><LucidePlus class="h-4 w-4" /></template>
        </Button>
      </div>
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
      <p v-if="loadError && tab !== 'store'" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

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
        <!-- Toolbar: bench selector + search + category filter -->
        <div class="mb-5 flex flex-wrap items-center gap-3">
          <div class="relative">
            <LucideServer class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
            <select
              v-model.number="catalogBenchId"
              class="fdm-focus rounded-lg border border-line bg-surface py-1.5 pl-9 pr-3 text-label text-ink-1 focus:border-line-strong"
              aria-label="Select bench for compatibility"
              @change="onBenchChange"
            >
              <option :value="null">All benches (no compat)</option>
              <option v-for="b in benches" :key="b.id" :value="b.id">{{ b.name }}</option>
            </select>
          </div>

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
        </div>

        <!-- Catalog unavailable -->
        <EmptyState
          v-if="catalogError"
          :icon="LucideServerOff"
          title="App store unavailable"
          :message="catalogError"
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
          <button
            v-for="app in filteredCatalog"
            :key="app.name"
            type="button"
            class="fdm-focus flex flex-col rounded-lg border bg-surface p-4 text-left transition hover:border-line-strong"
            :class="app.is_installable === false ? 'opacity-70' : 'border-line'"
            :aria-label="`View details for ${app.title}`"
            @click="openDetail(app)"
          >
            <!-- Card header: logo + title -->
            <div class="mb-3 flex w-full items-start gap-3">
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
                <div class="flex flex-wrap items-center gap-2">
                  <span class="truncate font-medium text-ink-1">{{ app.title }}</span>
                  <span
                    v-if="app.is_installable === true"
                    class="flex-none rounded-full border border-ok/40 bg-ok/10 px-1.5 py-0.5 text-meta font-medium text-ok"
                  >{{ app.latest_compatible_version ?? 'Compatible' }}</span>
                  <span
                    v-else-if="app.is_installable === false"
                    class="flex-none rounded-full border border-err/40 bg-err/10 px-1.5 py-0.5 text-meta font-medium text-err"
                  >Incompatible</span>
                </div>
                <div class="flex items-center gap-1.5 text-meta text-ink-3">
                  <LucideStar class="h-3 w-3" />
                  <span>{{ app.stars.toLocaleString() }}</span>
                  <span v-if="app.category">· {{ app.category }}</span>
                </div>
              </div>
            </div>

            <!-- Description -->
            <p class="mb-3 line-clamp-2 flex-1 text-label text-ink-2">{{ app.description }}</p>

            <!-- Incompatibility reason -->
            <p
              v-if="app.is_installable === false && app.reason"
              class="mb-2 rounded-md border border-warn/40 bg-warn/10 px-2.5 py-2 text-meta text-warn"
              role="note"
            >
              {{ app.reason }}
            </p>

            <!-- Categories -->
            <div v-if="app.categories.length" class="flex flex-wrap gap-1">
              <span
                v-for="cat in app.categories"
                :key="cat"
                class="rounded-full border border-line bg-raised px-2 py-0.5 text-meta text-ink-2"
              >{{ cat }}</span>
            </div>
          </button>
        </div>
      </template>
    </div>

    <!-- Add / edit source sheet -->
    <AddSourceSheet :open="sheetOpen" :source="editingSource" @close="sheetOpen = false" @saved="onSaved" />

    <!-- Delete source confirm -->
    <ConfirmModal
      v-model="deleteOpen"
      title="Delete source"
      :message="deleteTarget ? `Remove the app source &ldquo;${deleteTarget.name}&rdquo;.` : ''"
      verb="Delete source"
      variant="destructive"
      :loading="deleting"
      :consequences="[
        'Removes this source from the platform.',
        'Apps already installed from it are not touched.',
      ]"
      @confirm="confirmDelete"
    />

    <!-- App detail drawer -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-100 ease-in"
        leave-to-class="opacity-0"
      >
        <div
          v-if="detailOpen"
          class="fixed inset-0 z-50 flex justify-end bg-black/60"
          @click.self="closeDetail"
        >
          <Transition
            enter-active-class="transition duration-150 ease-out"
            enter-from-class="translate-x-full"
            leave-active-class="transition duration-100 ease-in"
            leave-to-class="translate-x-full"
            appear
          >
            <aside
              class="flex h-full w-full max-w-[600px] flex-col border-l border-line bg-base shadow-xl"
              role="dialog"
              aria-modal="true"
              :aria-label="detailApp?.title ?? 'App detail'"
            >
              <!-- Drawer header -->
              <header class="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
                <div class="flex min-w-0 items-start gap-3">
                  <div class="flex h-12 w-12 flex-none items-center justify-center overflow-hidden rounded-xl border border-line bg-raised">
                    <img
                      v-if="detailApp?.logo_url"
                      :src="detailApp.logo_url"
                      :alt="`${detailApp.title} logo`"
                      class="h-full w-full object-contain"
                    />
                    <LucidePackage v-else class="h-6 w-6 text-ink-3" />
                  </div>
                  <div class="min-w-0">
                    <h2 class="text-section font-semibold text-ink-1">{{ detailApp?.title }}</h2>
                    <div class="flex flex-wrap items-center gap-2 text-meta text-ink-3">
                      <span class="flex items-center gap-1">
                        <LucideStar class="h-3 w-3" />
                        {{ detailApp?.stars.toLocaleString() }}
                      </span>
                      <a
                        v-if="detailApp?.repo"
                        :href="detailApp.repo"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="fdm-focus flex items-center gap-1 rounded hover:text-ink-1"
                        @click.stop
                      >
                        <LucideGithub class="h-3.5 w-3.5" />
                        Repo
                      </a>
                      <a
                        v-if="detailApp?.documentation"
                        :href="detailApp.documentation"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="fdm-focus flex items-center gap-1 rounded hover:text-ink-1"
                        @click.stop
                      >
                        <LucideBookOpen class="h-3.5 w-3.5" />
                        Docs
                      </a>
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  class="fdm-focus flex-none rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
                  aria-label="Close"
                  @click="closeDetail"
                >
                  <LucideX class="h-4 w-4" />
                </button>
              </header>

              <!-- Drawer body -->
              <div class="min-h-0 flex-1 space-y-6 overflow-y-auto p-5">
                <!-- Loading skeleton -->
                <div v-if="detailLoading" class="space-y-3">
                  <div class="h-4 w-full animate-pulse rounded bg-raised" />
                  <div class="h-4 w-4/5 animate-pulse rounded bg-raised" />
                  <div class="mt-4 h-3 w-2/3 animate-pulse rounded bg-raised" />
                </div>

                <template v-else-if="detail">
                  <!-- Description -->
                  <p class="text-label text-ink-2">{{ detail.description }}</p>

                  <!-- Categories -->
                  <div v-if="detail.categories.length" class="flex flex-wrap gap-1.5">
                    <span
                      v-for="cat in detail.categories"
                      :key="cat"
                      class="rounded-full border border-line bg-raised px-2.5 py-0.5 text-meta text-ink-2"
                    >{{ cat }}</span>
                  </div>

                  <!-- Compatibility status -->
                  <div
                    v-if="detail.is_installable !== null"
                    class="rounded-lg border p-3.5"
                    :class="detail.is_installable ? 'border-ok/40 bg-ok/10' : 'border-err/40 bg-err/10'"
                  >
                    <div class="flex items-start gap-2">
                      <LucideCheckCircle2 v-if="detail.is_installable" class="mt-0.5 h-4 w-4 flex-none text-ok" />
                      <LucideXCircle v-else class="mt-0.5 h-4 w-4 flex-none text-err" />
                      <div class="min-w-0">
                        <p class="font-medium" :class="detail.is_installable ? 'text-ok' : 'text-err'">
                          {{
                            detail.is_installable
                              ? `Compatible — ${detail.latest_compatible_version ?? 'latest'}`
                              : 'Not installable on this bench'
                          }}
                        </p>
                        <p v-if="!detail.is_installable && detail.reason" class="mt-0.5 text-meta text-ink-2">
                          {{ detail.reason }}
                        </p>
                      </div>
                    </div>
                  </div>
                  <p v-else class="rounded-lg border border-line bg-raised px-3.5 py-3 text-label text-ink-3">
                    Select a bench in the toolbar to see compatibility.
                  </p>

                  <!-- Install plan -->
                  <section>
                    <h3 class="mb-3 text-label font-semibold text-ink-1">Install plan</h3>
                    <div
                      v-if="detail.plan_error"
                      class="rounded-lg border border-err/40 bg-err/10 px-3.5 py-3 text-label text-err"
                      role="alert"
                    >
                      <span class="font-medium">Cannot resolve plan: </span>{{ detail.plan_error }}
                    </div>
                    <p v-else-if="!detail.plan" class="text-label text-ink-3">
                      Select a bench to preview the install plan.
                    </p>
                    <ol v-else class="space-y-2">
                      <li
                        v-for="(step, i) in detail.plan"
                        :key="step.app"
                        class="flex items-start gap-3 rounded-lg border border-line bg-surface px-3.5 py-3"
                      >
                        <span class="mt-0.5 flex h-5 w-5 flex-none items-center justify-center rounded-full bg-raised text-meta font-medium text-ink-3">
                          {{ i + 1 }}
                        </span>
                        <div class="min-w-0 flex-1">
                          <div class="flex flex-wrap items-center gap-2">
                            <span class="font-medium text-ink-1">{{ step.app }}</span>
                            <span class="font-mono text-meta text-ink-3">{{ step.version }}</span>
                            <span class="rounded-full border border-line bg-raised px-1.5 py-0.5 font-mono text-meta text-ink-3">{{ step.channel }}</span>
                          </div>
                          <p class="mt-0.5 text-meta text-ink-3">{{ step.reason }} · branch {{ step.branch }}</p>
                        </div>
                      </li>
                    </ol>
                  </section>

                  <!-- Releases table -->
                  <section>
                    <h3 class="mb-3 text-label font-semibold text-ink-1">Releases</h3>
                    <p v-if="!detail.releases.length" class="text-label text-ink-3">No releases available.</p>
                    <div v-else class="overflow-hidden rounded-lg border border-line">
                      <table class="w-full text-left">
                        <thead>
                          <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                            <th class="px-3 py-2 font-medium">Version</th>
                            <th class="px-3 py-2 font-medium">Branch</th>
                            <th class="px-3 py-2 font-medium">Frappe</th>
                            <th class="px-3 py-2 font-medium">Channel</th>
                            <th v-if="catalogBenchId != null" class="px-3 py-2 font-medium">Compat</th>
                          </tr>
                        </thead>
                        <tbody class="divide-y divide-line text-label">
                          <tr v-for="r in detail.releases" :key="r.version">
                            <td class="px-3 py-2.5 font-mono text-ink-1">{{ r.version }}</td>
                            <td class="px-3 py-2.5 font-mono text-ink-2">{{ r.branch }}</td>
                            <td class="px-3 py-2.5 font-mono text-meta text-ink-3">{{ r.frappe_core }}</td>
                            <td class="px-3 py-2.5 text-ink-2">{{ r.channel }}</td>
                            <td v-if="catalogBenchId != null" class="px-3 py-2.5">
                              <span v-if="r.is_compatible === true" class="text-ok">✓</span>
                              <span v-else-if="r.is_compatible === false" class="text-err">✗</span>
                              <span v-else class="text-ink-3">—</span>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </section>
                </template>
              </div>

              <!-- Drawer footer: install -->
              <footer v-if="canManage" class="border-t border-line px-5 py-4">
                <div
                  v-if="installError"
                  class="mb-3 rounded-lg border border-err/40 bg-err/10 px-3 py-2.5 text-label text-err"
                  role="alert"
                >
                  {{ installError }}
                </div>
                <div class="flex items-end gap-3">
                  <div class="min-w-0 flex-1">
                    <label
                      class="mb-1 block text-meta font-medium uppercase tracking-wide text-ink-2"
                      for="drawer-site-pick"
                    >
                      Target site
                    </label>
                    <select
                      id="drawer-site-pick"
                      v-model.number="installSiteId"
                      class="fdm-focus w-full rounded-lg border border-line bg-base px-2.5 py-1.5 text-label text-ink-1 focus:border-line-strong"
                    >
                      <option :value="null" disabled>Select a site…</option>
                      <option v-for="s in installSites" :key="s.id" :value="s.id">{{ s.name }}</option>
                    </select>
                    <p v-if="sitesLoadError" class="mt-1 text-meta text-err" role="alert">{{ sitesLoadError }}</p>
                  </div>
                  <Button
                    variant="solid"
                    theme="gray"
                    label="Install"
                    :loading="installing"
                    :disabled="
                      installSiteId == null ||
                      installing ||
                      detailLoading ||
                      detail?.is_installable === false
                    "
                    @click="submitInstall"
                  />
                </div>
                <p v-if="detail?.is_installable === false" class="mt-2 text-meta text-warn">
                  This app is not installable on the selected bench — switch to a compatible bench to enable install.
                </p>
              </footer>
            </aside>
          </Transition>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import LucideBookOpen from '~icons/lucide/book-open'
import LucideCheckCircle2 from '~icons/lucide/check-circle-2'
import LucideGithub from '~icons/lucide/github'
import LucideLayoutGrid from '~icons/lucide/layout-grid'
import LucideLock from '~icons/lucide/lock'
import LucidePackage from '~icons/lucide/package'
import LucidePencil from '~icons/lucide/pencil'
import LucidePlus from '~icons/lucide/plus'
import LucideRefreshCw from '~icons/lucide/refresh-cw'
import LucideSearch from '~icons/lucide/search'
import LucideServer from '~icons/lucide/server'
import LucideServerOff from '~icons/lucide/server-off'
import LucideStar from '~icons/lucide/star'
import LucideTrash2 from '~icons/lucide/trash-2'
import LucideX from '~icons/lucide/x'
import LucideXCircle from '~icons/lucide/x-circle'
import { appsApi, type AppSource, type AppSourceKind, type InstalledApp } from '../api/apps'
import { benchesApi, type Bench } from '../api/benches'
import { marketplaceApi, type MarketplaceApp, type MarketplaceAppDetail } from '../api/marketplace'
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

// -- Source badge presentation -----------------------------------------------
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
  Array.from(new Set(installed.value.map((i) => i.app_name))).sort((a, b) =>
    a.localeCompare(b),
  ),
)
const matrixSites = computed(() =>
  Array.from(new Set(installed.value.map((i) => i.site_name))).sort((a, b) =>
    a.localeCompare(b),
  ),
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

// -- Store: catalog ----------------------------------------------------------
const catalog = ref<MarketplaceApp[]>([])
const catalogLoading = ref(false)
const catalogError = ref('')
const catalogSearch = ref('')
const catalogCategory = ref('')
const catalogBenchId = ref<number | null>(null)
const benches = ref<Bench[]>([])
const refreshing = ref(false)

const catalogCategories = computed(() => {
  const cats = new Set<string>()
  for (const app of catalog.value) for (const cat of app.categories) cats.add(cat)
  return Array.from(cats).sort()
})

const filteredCatalog = computed(() =>
  catalog.value
    .filter((app) => {
      const cat = catalogCategory.value
      const q = catalogSearch.value.trim().toLowerCase()
      if (cat && !app.categories.includes(cat)) return false
      if (
        q &&
        !app.name.toLowerCase().includes(q) &&
        !app.title.toLowerCase().includes(q) &&
        !app.description.toLowerCase().includes(q)
      )
        return false
      return true
    })
    .sort((a, b) => {
      if (b.stars !== a.stars) return b.stars - a.stars
      return a.title.localeCompare(b.title)
    }),
)

async function loadCatalog() {
  catalogLoading.value = true
  catalogError.value = ''
  try {
    catalog.value = await marketplaceApi.list({
      benchId: catalogBenchId.value ?? undefined,
    })
  } catch (error) {
    catalogError.value =
      error instanceof Error
        ? error.message
        : 'The app catalog is unavailable. Try again shortly.'
  } finally {
    catalogLoading.value = false
  }
}

async function loadBenches() {
  try {
    benches.value = await benchesApi.list()
  } catch {
    // Non-fatal — compat selector just stays empty
  }
}

async function refreshCatalog() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const result = await marketplaceApi.refresh()
    toast.success(
      result.refreshed
        ? `Catalog refreshed — ${result.app_count} apps.`
        : `Served from cache — ${result.app_count} apps (remote unreachable).`,
    )
    await loadCatalog()
  } catch (error) {
    toast.error(error instanceof Error ? error.message : 'Could not refresh the catalog.')
  } finally {
    refreshing.value = false
  }
}

function onBenchChange() {
  void loadCatalog()
}

// -- Store: app detail drawer ------------------------------------------------
const detailOpen = ref(false)
const detailLoading = ref(false)
const detailApp = ref<MarketplaceApp | null>(null)
const detail = ref<MarketplaceAppDetail | null>(null)

const installSiteId = ref<number | null>(null)
const installSites = ref<Site[]>([])
const sitesLoadError = ref('')
const installing = ref(false)
const installError = ref('')

function openDetail(app: MarketplaceApp) {
  detailApp.value = app
  detail.value = null
  detailOpen.value = true
  installSiteId.value = null
  installError.value = ''
  void loadDetail(app.name)
  void loadSitesForDrawer()
}

function closeDetail() {
  if (installing.value) return
  detailOpen.value = false
}

async function loadDetail(name: string) {
  detailLoading.value = true
  try {
    detail.value = await marketplaceApi.detail(name, catalogBenchId.value ?? undefined)
  } catch (error) {
    detail.value = null
    toast.error(error instanceof Error ? error.message : 'Could not load app details.')
  } finally {
    detailLoading.value = false
  }
}

async function loadSitesForDrawer() {
  sitesLoadError.value = ''
  try {
    installSites.value = await sitesApi.list(
      catalogBenchId.value != null ? catalogBenchId.value : undefined,
    )
  } catch (error) {
    sitesLoadError.value = error instanceof Error ? error.message : 'Could not load sites.'
  }
}

async function submitInstall() {
  const app = detail.value ?? detailApp.value
  if (!app || installSiteId.value == null || installing.value) return
  installing.value = true
  installError.value = ''
  try {
    const job = await marketplaceApi.install(installSiteId.value, { app: app.name })
    detailOpen.value = false
    toast.success(`Installing ${app.title ?? app.name} — job #${job.id} queued.`)
  } catch (error) {
    installError.value =
      error instanceof Error ? error.message : 'Could not start the install.'
  } finally {
    installing.value = false
  }
}

// -- Tab switching -----------------------------------------------------------
function switchTab(key: Tab) {
  tab.value = key
  if (key === 'store' && catalog.value.length === 0 && !catalogLoading.value) {
    void loadBenches()
    void loadCatalog()
  }
}

// -- Initial load ------------------------------------------------------------
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
