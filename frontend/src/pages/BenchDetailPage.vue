<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center gap-3 border-b border-line px-8 py-5">
      <button
        type="button"
        class="fdm-focus rounded p-1 text-ink-3 transition hover:bg-raised hover:text-ink-1"
        aria-label="Back to benches"
        @click="router.push('/benches')"
      >
        <LucideArrowLeft class="h-4 w-4" />
      </button>
      <StatusDot v-if="bench" :status="benchDot(bench.status)" />
      <h1 class="truncate text-lg font-semibold text-ink-1">{{ bench?.name ?? 'Bench' }}</h1>
      <StatusBadge
        v-if="bench"
        :status="bench.is_production ? 'running' : 'muted'"
        :label="bench.is_production ? 'Production' : 'Dev'"
      />
    </header>

    <!-- Tab bar (skeleton: only Overview is wired this session). -->
    <nav class="flex gap-1 border-b border-line px-8" aria-label="Bench sections">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :disabled="tab.key !== 'overview'"
        class="fdm-focus -mb-px border-b-2 px-3 py-2.5 text-label transition"
        :class="tab.key === activeTab
          ? 'border-ink-1 font-medium text-ink-1'
          : 'border-transparent text-ink-3 disabled:opacity-40'"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>

      <div v-else-if="loading" class="max-w-3xl rounded-lg border border-line bg-surface">
        <div class="border-b border-line px-4 py-2.5"><div class="h-3.5 w-24 animate-pulse rounded bg-raised" /></div>
        <div class="divide-y divide-line">
          <div v-for="i in 8" :key="i" class="flex items-center justify-between px-4 py-2">
            <div class="h-3 w-28 animate-pulse rounded bg-raised" />
            <div class="h-3 w-40 animate-pulse rounded bg-raised" />
          </div>
        </div>
      </div>

      <div v-else-if="bench && activeTab === 'overview'" class="grid max-w-4xl gap-6 lg:grid-cols-2">
        <!-- Parsed config: identity + versions -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Overview</h2>
          <dl class="divide-y divide-line">
            <div v-for="spec in specs" :key="spec.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ spec.label }}</dt>
              <dd class="max-w-[62%] truncate text-label text-ink-1" :title="spec.value">{{ spec.value }}</dd>
            </div>
          </dl>
        </section>

        <!-- Parsed config: port map from common_site_config.json -->
        <section class="rounded-lg border border-line bg-surface">
          <h2 class="border-b border-line px-4 py-2.5 text-label font-semibold text-ink-1">Ports</h2>
          <dl class="divide-y divide-line">
            <div v-for="p in portList" :key="p.label" class="flex items-center justify-between px-4 py-2">
              <dt class="text-meta uppercase tracking-wide text-ink-3">{{ p.label }}</dt>
              <dd class="font-mono text-label text-ink-1">{{ p.value ?? '—' }}</dd>
            </div>
          </dl>
          <p class="px-4 py-2 text-meta text-ink-3">Parsed from sites/common_site_config.json.</p>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LucideArrowLeft from '~icons/lucide/arrow-left'
import { benchesApi, type Bench } from '../api/benches'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import { benchStatusDot as benchDot, portRows, versionChip } from '../lib/benches'
import { absoluteTime, relativeTime } from '../lib/servers'

const route = useRoute()
const router = useRouter()
const benchId = Number(route.params.id)

const bench = ref<Bench | null>(null)
const loading = ref(true)
const loadError = ref('')
const activeTab = ref('overview')

// Skeleton tab bar — Sites/Apps/Config land in later sessions.
const tabs = [
  { key: 'overview', label: 'Overview' },
  { key: 'sites', label: 'Sites' },
  { key: 'apps', label: 'Apps' },
  { key: 'config', label: 'Config' },
]

const specs = computed(() => {
  const b = bench.value
  if (!b) return []
  return [
    { label: 'Frappe', value: `${versionChip(b.frappe_version)} (${b.frappe_version ?? 'unknown'})` },
    { label: 'Python', value: b.python_version ?? '—' },
    { label: 'Node', value: b.node_version ?? '—' },
    { label: 'Mode', value: b.is_production ? 'Production' : 'Dev' },
    { label: 'Status', value: b.status === 'active' ? 'Active' : 'Missing' },
    { label: 'Path', value: b.path },
    { label: 'Discovered', value: b.discovered_at ? `${relativeTime(b.discovered_at)} (${absoluteTime(b.discovered_at)})` : 'never' },
  ]
})

const portList = computed(() => (bench.value ? portRows(bench.value) : []))

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    bench.value = await benchesApi.get(benchId)
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load this bench.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
