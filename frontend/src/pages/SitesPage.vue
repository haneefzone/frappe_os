<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Sites</h1>
        <p class="text-meta text-ink-2">Frappe sites across all benches.</p>
      </div>
      <Button
        v-if="canCreate"
        variant="solid"
        theme="gray"
        label="Create site"
        @click="router.push('/sites/new')"
      >
        <template #prefix><LucidePlus class="h-4 w-4" /></template>
      </Button>
    </header>

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

      <EmptyState
        v-else-if="sites.length === 0"
        :icon="LucideGlobe"
        title="No sites yet"
        message="Discover a bench to inventory its sites, or create a new site on a v16 bench."
        :cta-label="canCreate ? 'Create site' : undefined"
        @cta="router.push('/sites/new')"
      />

      <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
        <table class="w-full text-left">
          <thead>
            <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
              <th class="px-4 py-2 font-medium">Site</th>
              <th class="px-4 py-2 font-medium">Bench</th>
              <th class="px-4 py-2 font-medium">Environment</th>
              <th class="px-4 py-2 font-medium">Health</th>
              <th class="px-4 py-2 font-medium">Scheduler</th>
              <th class="px-4 py-2 font-medium">Maintenance</th>
              <th class="px-4 py-2 font-medium">Status</th>
              <th class="px-4 py-2 font-medium">Created</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line text-label">
            <tr v-for="site in sites" :key="site.id">
              <td class="px-4 py-2.5">
                <button
                  type="button"
                  class="fdm-focus rounded font-medium text-ink-1 hover:underline"
                  @click="router.push(`/sites/${site.id}`)"
                >
                  {{ site.name }}
                </button>
                <div class="truncate font-mono text-meta text-ink-3">{{ site.server_name }}</div>
              </td>
              <td class="px-4 py-2.5">
                <button
                  type="button"
                  class="fdm-focus rounded text-ink-2 hover:text-ink-1 hover:underline"
                  @click="router.push(`/benches/${site.bench_id}`)"
                >
                  {{ site.bench_name }}
                </button>
              </td>
              <td class="px-4 py-2.5"><EnvironmentBadge :env="site.server_env_tag" /></td>
              <td class="px-4 py-2.5">
                <span class="flex items-center gap-2">
                  <StatusDot :status="healthDot(site.health)" />
                  <span class="text-ink-2">{{ healthLabel[site.health] }}</span>
                </span>
              </td>
              <td class="px-4 py-2.5">
                <StatusBadge
                  :status="site.scheduler_enabled ? 'running' : 'muted'"
                  :label="schedulerLabel(site.scheduler_enabled)"
                />
              </td>
              <td class="px-4 py-2.5">
                <StatusBadge
                  v-if="site.maintenance_mode"
                  status="warn"
                  label="On"
                />
                <span v-else class="text-ink-3">Off</span>
              </td>
              <td class="px-4 py-2.5">
                <span class="flex items-center gap-2">
                  <StatusDot :status="siteDot(site.status)" />
                  <span class="text-ink-2">{{ statusLabel[site.status] }}</span>
                </span>
              </td>
              <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(site.created_at)">
                {{ relativeTime(site.created_at) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import LucideGlobe from '~icons/lucide/globe'
import LucidePlus from '~icons/lucide/plus'
import { sitesApi, type Site } from '../api/sites'
import EmptyState from '../components/EmptyState.vue'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import StatusBadge from '../components/StatusBadge.vue'
import StatusDot from '../components/StatusDot.vue'
import {
  HEALTH_LABEL as healthLabel,
  SITE_STATUS_LABEL as statusLabel,
  healthDot,
  schedulerLabel,
  siteStatusDot as siteDot,
} from '../lib/sites'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
// Creating a site launches a site.create job (site:operate) — the same gate the
// backend enforces; hide the button for roles that would only get a 403.
const canCreate = auth.hasPermission('site:operate')

const sites = ref<Site[]>([])
const loading = ref(true)
const loadError = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    sites.value = await sitesApi.list()
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load sites.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
