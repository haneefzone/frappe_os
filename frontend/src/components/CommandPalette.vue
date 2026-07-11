<template>
  <!-- ⌘K command palette overlay -->
  <Teleport to="body">
    <Transition
      enter-active-class="transition-opacity duration-150 ease-out"
      leave-active-class="transition-opacity duration-150 ease-out"
      enter-from-class="opacity-0"
      leave-to-class="opacity-0"
    >
      <div
        v-if="open"
        class="fixed inset-0 z-50 flex items-start justify-center pt-20"
        :style="{ background: 'rgba(0,0,0,0.6)' }"
        @mousedown.self="close"
      >
        <div
          ref="paletteEl"
          class="flex w-full max-w-xl flex-col rounded-xl border shadow-2xl"
          :style="{
            background: 'var(--bg-raised)',
            borderColor: 'var(--border-strong)',
          }"
          role="dialog"
          aria-label="Command palette"
          aria-modal="true"
        >
          <!-- Search input -->
          <div
            class="flex items-center gap-3 border-b px-4 py-3"
            :style="{ borderColor: 'var(--border)' }"
          >
            <LucideSearch class="h-4 w-4 shrink-0" :style="{ color: 'var(--text-secondary)' }" />
            <input
              ref="inputEl"
              v-model="query"
              type="text"
              class="fdm-focus flex-1 bg-transparent text-sm focus-visible:outline-none"
              :style="{ color: 'var(--text-primary)' }"
              placeholder="Search servers, sites, jobs, actions…"
              aria-label="Search"
              autocomplete="off"
              spellcheck="false"
              @keydown.escape="onEscape"
              @keydown.arrow-down.prevent="moveDown"
              @keydown.arrow-up.prevent="moveUp"
              @keydown.enter.prevent="activateSelected"
            />
            <kbd
              class="shrink-0 rounded border px-1.5 py-0.5 text-[10px]"
              :style="{ borderColor: 'var(--border-strong)', color: 'var(--text-secondary)' }"
            >
              ESC
            </kbd>
          </div>

          <!-- Confirm step: an allowed NL action must be confirmed before it runs -->
          <div v-if="pendingProposal" class="px-4 py-4">
            <div class="flex items-start gap-3">
              <LucideSparkles class="mt-0.5 h-4 w-4 shrink-0 text-run" />
              <div class="min-w-0 flex-1">
                <p class="text-sm font-medium" :style="{ color: 'var(--text-primary)' }">
                  {{ pendingProposal.title }}
                </p>
                <p class="mt-0.5 text-xs" :style="{ color: 'var(--text-secondary)' }">
                  {{ pendingProposal.summary }}
                </p>
                <div class="mt-2 flex flex-wrap items-center gap-1.5">
                  <span
                    class="rounded border px-2 py-0.5 font-mono text-[11px]"
                    :style="{ borderColor: 'var(--border)', color: 'var(--text-primary)' }"
                  >
                    {{ pendingProposal.action_name }}
                  </span>
                  <span
                    v-for="[key, value] in Object.entries(pendingProposal.params)"
                    :key="key"
                    class="rounded border px-2 py-0.5 font-mono text-[11px]"
                    :style="{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }"
                  >
                    {{ key }}=<span :style="{ color: 'var(--text-primary)' }">{{ value }}</span>
                  </span>
                </div>
                <p v-if="confirmError" class="mt-2 text-xs text-err" role="alert">
                  {{ confirmError }}
                </p>
              </div>
            </div>
            <div class="mt-4 flex items-center justify-end gap-2">
              <Button variant="subtle" theme="gray" label="Cancel" :disabled="running" @click="cancelProposal" />
              <Button
                variant="solid"
                theme="gray"
                :label="running ? 'Running…' : `Run ${pendingProposal.action_name}`"
                :loading="running"
                @click="runProposal"
              />
            </div>
          </div>

          <!-- Results list -->
          <div v-else ref="listEl" class="max-h-96 overflow-y-auto py-2" aria-live="polite" aria-atomic="false">
            <span class="sr-only" aria-live="polite">{{ resultStatusText }}</span>
            <template v-if="loading">
              <div class="px-4 py-3 text-sm" :style="{ color: 'var(--text-secondary)' }">
                Searching…
              </div>
            </template>
            <template v-else-if="results.length === 0 && !hasAiContent && query.trim()">
              <div class="px-4 py-3 text-sm" :style="{ color: 'var(--text-secondary)' }">
                No results for "{{ query }}"
              </div>
            </template>
            <template v-else>
              <!-- Group by kind -->
              <template v-for="group in groupedResults" :key="group.kind">
                <div
                  class="px-3 py-1 text-[11px] font-medium uppercase tracking-wide"
                  :style="{ color: 'var(--text-secondary)' }"
                >
                  {{ kindLabel(group.kind) }}
                </div>
                <button
                  v-for="(item, idx) in group.items"
                  :key="item.id"
                  type="button"
                  class="fdm-focus flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-sm transition-colors duration-100"
                  :class="
                    flatIndex(group.kind, idx) === activeIndex
                      ? 'bg-white/10 text-white'
                      : 'hover:bg-white/5'
                  "
                  :style="
                    flatIndex(group.kind, idx) === activeIndex
                      ? {}
                      : { color: 'var(--text-primary)' }
                  "
                  @mouseenter="activeIndex = flatIndex(group.kind, idx)"
                  @click="activate(item)"
                >
                  <component
                    :is="kindIcon(item.kind)"
                    class="h-4 w-4 shrink-0"
                    :style="{ color: 'var(--text-secondary)' }"
                  />
                  <span class="min-w-0 flex-1">
                    <span class="block truncate font-medium">{{ item.title }}</span>
                    <span
                      v-if="item.subtitle"
                      class="block truncate text-xs"
                      :style="{ color: 'var(--text-secondary)' }"
                    >
                      {{ item.subtitle }}
                    </span>
                  </span>
                  <kbd
                    v-if="flatIndex(group.kind, idx) === activeIndex"
                    class="shrink-0 rounded border px-1.5 py-0.5 text-[10px]"
                    :style="{ borderColor: 'var(--border-strong)', color: 'var(--text-secondary)' }"
                  >
                    Enter
                  </kbd>
                </button>
              </template>

              <!-- AI actions (uiux-spec §17: natural-language command palette) -->
              <template v-if="aiLoading">
                <div
                  class="px-3 py-1 text-[11px] font-medium uppercase tracking-wide"
                  :style="{ color: 'var(--text-secondary)' }"
                >
                  AI actions
                </div>
                <div class="flex items-center gap-2 px-3 py-2.5 text-sm" :style="{ color: 'var(--text-secondary)' }">
                  <LucideLoader2 class="h-4 w-4 animate-spin text-run" />
                  Interpreting…
                </div>
              </template>
              <template v-else-if="proposals.length">
                <div
                  class="px-3 py-1 text-[11px] font-medium uppercase tracking-wide"
                  :style="{ color: 'var(--text-secondary)' }"
                >
                  AI actions
                </div>
                <button
                  v-for="(proposal, idx) in proposals"
                  :key="`ai:${idx}`"
                  type="button"
                  :disabled="!proposal.allowed"
                  class="fdm-focus flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-sm transition-colors duration-100"
                  :class="[
                    proposalActive(idx) === activeIndex ? 'bg-white/10 text-white' : 'hover:bg-white/5',
                    proposal.allowed ? '' : 'cursor-not-allowed opacity-50',
                  ]"
                  :style="proposalActive(idx) === activeIndex ? {} : { color: 'var(--text-primary)' }"
                  @mouseenter="proposal.allowed && (activeIndex = proposalActive(idx))"
                  @click="selectProposal(proposal)"
                >
                  <LucideSparkles
                    class="h-4 w-4 shrink-0"
                    :class="proposal.allowed ? 'text-run' : ''"
                    :style="proposal.allowed ? {} : { color: 'var(--text-secondary)' }"
                  />
                  <span class="min-w-0 flex-1">
                    <span class="block truncate font-medium">{{ proposal.title }}</span>
                    <span class="block truncate text-xs" :style="{ color: 'var(--text-secondary)' }">
                      {{ proposal.allowed ? proposal.summary : 'Your role can’t run this action' }}
                    </span>
                  </span>
                  <kbd
                    v-if="proposal.allowed && proposalActive(idx) === activeIndex"
                    class="shrink-0 rounded border px-1.5 py-0.5 text-[10px]"
                    :style="{ borderColor: 'var(--border-strong)', color: 'var(--text-secondary)' }"
                  >
                    Enter
                  </kbd>
                </button>
              </template>
              <template v-else-if="aiReason">
                <div
                  class="px-3 py-1 text-[11px] font-medium uppercase tracking-wide"
                  :style="{ color: 'var(--text-secondary)' }"
                >
                  AI actions
                </div>
                <div class="px-3 py-2 text-xs" :style="{ color: 'var(--text-secondary)' }">
                  {{ aiReason }}
                </div>
              </template>
            </template>
          </div>

          <!-- Footer hint -->
          <div
            class="flex flex-wrap items-center gap-x-4 gap-y-1 border-t px-4 py-2 text-[11px]"
            :style="{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }"
          >
            <span><kbd class="font-mono">↑↓</kbd> navigate</span>
            <span><kbd class="font-mono">↵</kbd> open</span>
            <span><kbd class="font-mono">Esc</kbd> dismiss</span>
            <span class="ml-auto flex flex-wrap gap-x-3">
              <span><kbd class="font-mono">g d</kbd> dashboard</span>
              <span><kbd class="font-mono">g j</kbd> jobs</span>
              <span><kbd class="font-mono">t</kbd> terminal</span>
              <span><kbd class="font-mono">/</kbd> search</span>
            </span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useFocusTrap } from '../composables/useFocusTrap'
import LucideActivity from '~icons/lucide/activity'
import LucideArrowRight from '~icons/lucide/arrow-right'
import LucideCalendar from '~icons/lucide/calendar'
import LucideLayoutDashboard from '~icons/lucide/layout-dashboard'
import LucideLoader2 from '~icons/lucide/loader-2'
import LucideSearch from '~icons/lucide/search'
import LucideServer from '~icons/lucide/server'
import LucideSparkles from '~icons/lucide/sparkles'
import LucideTerminal from '~icons/lucide/terminal'
import LucideZap from '~icons/lucide/zap'
import { type SearchResult, searchApi } from '../api/notifications'
import { ApiError, apiClient } from '../api/client'
import { type NLProposal, copilotApi } from '../api/copilot'
import { toast } from './toast'

const router = useRouter()

const open = ref(false)
const query = ref('')
const results = ref<SearchResult[]>([])
const loading = ref(false)
const activeIndex = ref(0)
const inputEl = ref<HTMLInputElement | null>(null)
const paletteEl = ref<HTMLElement | undefined>()
const listEl = ref<HTMLElement | null>(null)

const { activate: trapActivate, deactivate: trapDeactivate } = useFocusTrap(paletteEl)

let _debounce: ReturnType<typeof setTimeout> | null = null

// --- AI actions (natural-language resolution, uiux-spec §17) -------------- //
const proposals = ref<NLProposal[]>([])
const aiLoading = ref(false)
const aiReason = ref('')
const pendingProposal = ref<NLProposal | null>(null)
const confirmError = ref('')
const running = ref(false)
let _aiDebounce: ReturnType<typeof setTimeout> | null = null
let _aiSeq = 0

const hasAiContent = computed(
  () => aiLoading.value || proposals.value.length > 0 || aiReason.value !== '',
)

// Only allowed proposals are keyboard-selectable; they follow the static rows.
const allowedProposals = computed(() => proposals.value.filter((p) => p.allowed))

/** Flat index of an allowed proposal at `idx` in the full proposals array. */
function proposalActive(idx: number): number {
  const proposal = proposals.value[idx]
  if (!proposal?.allowed) return -1
  const before = proposals.value.slice(0, idx).filter((p) => p.allowed).length
  return flatGroups.value.length + before
}

const resultStatusText = computed(() => {
  if (loading.value) return 'Searching'
  if (!query.value.trim()) return ''
  if (results.value.length === 0) return 'No results'
  return `${results.value.length} result${results.value.length === 1 ? '' : 's'}`
})

// Group results by kind for display.
const KIND_ORDER = ['nav', 'server', 'bench', 'site', 'job', 'schedule', 'action'] as const

const groupedResults = computed(() => {
  const groups: { kind: string; items: SearchResult[] }[] = []
  const byKind = new Map<string, SearchResult[]>()
  for (const r of results.value) {
    if (!byKind.has(r.kind)) byKind.set(r.kind, [])
    byKind.get(r.kind)!.push(r)
  }
  for (const kind of KIND_ORDER) {
    const items = byKind.get(kind)
    if (items && items.length) groups.push({ kind, items })
  }
  return groups
})

// Flat index across all groups for keyboard nav.
const flatGroups = computed(() => {
  const out: SearchResult[] = []
  for (const g of groupedResults.value) out.push(...g.items)
  return out
})

function flatIndex(kind: string, idx: number): number {
  let n = 0
  for (const g of groupedResults.value) {
    if (g.kind === kind) return n + idx
    n += g.items.length
  }
  return n + idx
}

function kindLabel(kind: string): string {
  const map: Record<string, string> = {
    nav: 'Navigate',
    server: 'Servers',
    bench: 'Benches',
    site: 'Sites',
    job: 'Jobs',
    schedule: 'Schedules',
    action: 'Actions',
  }
  return map[kind] ?? kind
}

function kindIcon(kind: string) {
  const map: Record<string, unknown> = {
    nav: LucideLayoutDashboard,
    server: LucideServer,
    bench: LucideTerminal,
    site: LucideActivity,
    job: LucideZap,
    schedule: LucideCalendar,
    action: LucideArrowRight,
  }
  return map[kind] ?? LucideSearch
}

watch(query, (v) => {
  if (_debounce) clearTimeout(_debounce)
  _debounce = setTimeout(() => void doSearch(v), 180)
  // A fresh query voids any in-flight confirm step and prior AI proposals.
  pendingProposal.value = null
  confirmError.value = ''
  scheduleResolve(v)
  activeIndex.value = 0
})

async function doSearch(q: string) {
  loading.value = true
  try {
    const data = await searchApi.search(q)
    results.value = data.results
  } catch {
    results.value = []
  } finally {
    loading.value = false
  }
}

/** Debounced NL resolution — only fires for queries of 3+ chars. */
function scheduleResolve(q: string) {
  if (_aiDebounce) clearTimeout(_aiDebounce)
  const trimmed = q.trim()
  if (trimmed.length < 3) {
    proposals.value = []
    aiReason.value = ''
    aiLoading.value = false
    return
  }
  _aiDebounce = setTimeout(() => void resolveNL(trimmed), 250)
}

async function resolveNL(q: string) {
  const seq = ++_aiSeq
  aiLoading.value = true
  try {
    const data = await copilotApi.resolvePalette(q)
    if (seq !== _aiSeq) return // a newer query superseded this one
    proposals.value = data.resolved ? data.proposals : []
    // Show the reason only when nothing resolved (never over a real result set).
    aiReason.value = data.resolved ? '' : (data.reason ?? '')
  } catch {
    if (seq !== _aiSeq) return
    proposals.value = []
    aiReason.value = ''
  } finally {
    if (seq === _aiSeq) aiLoading.value = false
  }
}

// Navigable rows = static results, then keyboard-selectable AI proposals.
const navCount = computed(() => flatGroups.value.length + allowedProposals.value.length)

function moveDown() {
  activeIndex.value = Math.min(activeIndex.value + 1, navCount.value - 1)
  scrollActiveIntoView()
}

function moveUp() {
  activeIndex.value = Math.max(activeIndex.value - 1, 0)
  scrollActiveIntoView()
}

function scrollActiveIntoView() {
  nextTick(() => {
    const btn = listEl.value?.querySelectorAll('button')[activeIndex.value]
    btn?.scrollIntoView({ block: 'nearest' })
  })
}

function activateSelected() {
  if (pendingProposal.value) {
    void runProposal()
    return
  }
  const item = flatGroups.value[activeIndex.value]
  if (item) {
    activate(item)
    return
  }
  // Beyond the static rows: an allowed AI proposal is selected.
  const proposal = allowedProposals.value[activeIndex.value - flatGroups.value.length]
  if (proposal) selectProposal(proposal)
}

/** Open the confirm step for an allowed proposal (never runs directly). */
function selectProposal(proposal: NLProposal) {
  if (!proposal.allowed) return
  confirmError.value = ''
  pendingProposal.value = proposal
}

function cancelProposal() {
  pendingProposal.value = null
  confirmError.value = ''
  nextTick(() => inputEl.value?.focus())
}

async function runProposal() {
  const proposal = pendingProposal.value
  if (!proposal || running.value) return
  running.value = true
  confirmError.value = ''
  try {
    // Execute exactly the server-authored run — never a locally built command.
    await apiClient.post(proposal.run.path, proposal.run.body)
    close()
    toast.success(`Started: ${proposal.title}`)
  } catch (e) {
    confirmError.value = e instanceof ApiError ? e.message : 'Could not run the action.'
  } finally {
    running.value = false
  }
}

/** Escape cancels an open confirm step first, otherwise dismisses the palette. */
function onEscape() {
  if (pendingProposal.value) cancelProposal()
  else close()
}

async function activate(item: SearchResult) {
  close()
  if (item.action) {
    // Dispatch as a real CommandJob (golden rule 1 — never raw shell).
    try {
      const job = await apiClient.post<{ id: number }>('/api/jobs', item.action)
      router.push(`/jobs/${job.id}`)
    } catch (e: unknown) {
      console.error('palette action failed', e)
    }
  } else if (item.url) {
    router.push(item.url)
  }
  addRecent(item)
}

// Recents stored in localStorage; max 6 entries.
const RECENT_KEY = 'fdm-palette-recents'

function addRecent(item: SearchResult) {
  try {
    const existing: SearchResult[] = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
    const filtered = existing.filter((r) => r.id !== item.id)
    const updated = [item, ...filtered].slice(0, 6)
    localStorage.setItem(RECENT_KEY, JSON.stringify(updated))
  } catch {
    // storage unavailable — ignore
  }
}

function loadRecents(): SearchResult[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
  } catch {
    return []
  }
}

function openPalette() {
  open.value = true
  query.value = ''
  activeIndex.value = 0
  resetAi()
  // Show recents immediately.
  const recents = loadRecents()
  results.value = recents.length
    ? recents
    : [
        { kind: 'nav', id: 'nav:dashboard', title: 'Dashboard', url: '/' },
        { kind: 'nav', id: 'nav:jobs', title: 'Jobs', url: '/jobs' },
      ]
  nextTick(() => {
    inputEl.value?.focus()
    trapActivate()
  })
}

function resetAi() {
  if (_aiDebounce) clearTimeout(_aiDebounce)
  _aiSeq++
  proposals.value = []
  aiReason.value = ''
  aiLoading.value = false
  pendingProposal.value = null
  confirmError.value = ''
  running.value = false
}

function close() {
  trapDeactivate()
  open.value = false
  query.value = ''
  results.value = []
  resetAi()
}

// Expose open() for external callers (AppTopbar bind, keyboard shortcuts).
// showShortcuts() opens the palette in shortcut-legend mode (? key).
function showShortcuts() {
  openPalette()
}

defineExpose({ openPalette, close, showShortcuts })
</script>
