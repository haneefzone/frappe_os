<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Tools</h1>
        <p class="text-meta text-ink-2">
          Stack checklist: detected vs recommended versions, one-click installs.
        </p>
      </div>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-4 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- No servers at all -->
      <EmptyState
        v-else-if="!loading && servers.length === 0"
        :icon="LucideServer"
        title="No servers yet"
        message="Register a server first, then come back to check its tool stack."
        cta-label="Go to Servers"
        @cta="router.push('/servers')"
      />

      <template v-else>
        <!-- Server selector (shown once servers load) -->
        <div v-if="!loading && servers.length > 1" class="mb-6">
          <label class="mb-1.5 block text-label font-medium text-ink-2">Server</label>
          <div role="tablist" class="flex flex-wrap gap-2">
            <button
              v-for="s in servers"
              :key="s.id"
              role="tab"
              type="button"
              :aria-selected="selectedId === s.id"
              class="fdm-focus flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-label transition"
              :class="
                selectedId === s.id
                  ? 'border-line-strong bg-raised text-ink-1'
                  : 'border-line bg-surface text-ink-2 hover:bg-raised'
              "
              @click="selectedId = s.id"
            >
              <StatusDot :status="statusDot(s.status)" size="sm" />
              {{ s.name }}
            </button>
          </div>
        </div>

        <!-- Single-server label when there's only one -->
        <div v-else-if="!loading && servers.length === 1" class="mb-4 flex items-center gap-2">
          <StatusDot :status="statusDot(servers[0].status)" />
          <h2 class="text-section font-semibold text-ink-1">{{ servers[0].name }}</h2>
          <EnvironmentBadge :env="servers[0].env_tag" />
        </div>

        <!-- Loading skeleton for server list -->
        <div v-if="loading" class="mb-6 flex gap-2">
          <div v-for="i in 3" :key="i" class="h-8 w-28 animate-pulse rounded-lg bg-raised" />
        </div>

        <!-- Checklist for the selected server -->
        <ToolsChecklist v-if="selectedId !== null" :server-id="selectedId" />
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import LucideServer from '~icons/lucide/server'
import { serversApi, type Server } from '../api/servers'
import EnvironmentBadge from '../components/EnvironmentBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusDot from '../components/StatusDot.vue'
import ToolsChecklist from '../components/ToolsChecklist.vue'
import { statusDot } from '../lib/servers'

const router = useRouter()

const servers = ref<Server[]>([])
const loading = ref(true)
const loadError = ref('')
const selectedId = ref<number | null>(null)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    servers.value = await serversApi.list()
    if (servers.value.length > 0 && selectedId.value === null) {
      selectedId.value = servers.value[0].id
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load servers.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
