<template>
  <div class="flex h-full flex-col">
    <!-- Access-denied guard (Developer+ only) -->
    <template v-if="!canAccess">
      <div class="flex h-full flex-col items-center justify-center gap-3 text-ink-3">
        <LucideLock class="h-8 w-8" />
        <p class="text-body font-medium text-ink-1">Terminal access requires Developer role or higher.</p>
      </div>
    </template>

    <template v-else>
      <!-- Header: new-session picker + tab bar -->
      <header class="border-b border-line">
        <div class="flex items-center gap-4 px-6 py-4">
          <div class="flex min-w-0 items-center gap-2">
            <LucideTerminal class="h-5 w-5 shrink-0 text-ink-3" />
            <h1 class="text-lg font-semibold text-ink-1">Terminal</h1>
          </div>

          <div class="flex items-center gap-2">
            <select
              v-model="selectedServerId"
              class="
                fdm-focus rounded border border-line bg-surface px-3 py-1.5 text-label
                text-ink-1 focus:border-border-strong
              "
              :disabled="loadingServers || openingSession"
            >
              <option :value="null" disabled>Select server…</option>
              <option v-for="s in servers" :key="s.id" :value="s.id">
                {{ s.name }} ({{ s.hostname }})
              </option>
            </select>
            <Button
              label="Open terminal"
              variant="solid"
              theme="gray"
              :disabled="selectedServerId == null || openingSession"
              :loading="openingSession"
              @click="openSession"
            >
              <template #prefix><LucidePlus class="h-4 w-4" /></template>
            </Button>
          </div>

          <p v-if="sessionError" class="text-meta text-error" role="alert">{{ sessionError }}</p>
        </div>

        <!-- Tab strip -->
        <div v-if="tabs.length" class="flex min-w-0 items-end gap-0 overflow-x-auto px-6">
          <button
            v-for="(tab, i) in tabs"
            :key="tab.id"
            type="button"
            :class="[
              'fdm-focus group flex shrink-0 items-center gap-2 border-b-2 px-4 py-2 text-label transition',
              tab.id === activeTabId
                ? 'border-ink-1 text-ink-1'
                : 'border-transparent text-ink-3 hover:text-ink-2',
            ]"
            @click="activeTabId = tab.id"
          >
            <span class="truncate max-w-[160px]">{{ tab.label }}</span>
            <span
              v-if="tab.idleWarning"
              class="text-warn text-meta"
              title="Idle timeout approaching"
            >⏱</span>
            <span
              v-if="tab.status === 'disconnected'"
              class="text-error text-meta"
            >✕</span>
            <button
              type="button"
              class="fdm-focus ml-1 rounded opacity-0 transition hover:text-error group-hover:opacity-100"
              aria-label="Close tab"
              @click.stop="closeTab(i)"
            >
              <LucideX class="h-3.5 w-3.5" />
            </button>
          </button>
        </div>
      </header>

      <!-- Empty state -->
      <div
        v-if="!tabs.length"
        class="flex flex-1 flex-col items-center justify-center gap-3 text-ink-3"
      >
        <LucideTerminal class="h-10 w-10 opacity-40" />
        <p class="text-body">Select a server above and click <strong>Open terminal</strong>.</p>
      </div>

      <!-- Terminal panels (one per tab, hidden when not active) -->
      <div v-else class="relative min-h-0 flex-1">
        <TerminalTab
          v-for="tab in tabs"
          :key="tab.id"
          :tab="tab"
          :active="tab.id === activeTabId"
          @idle-warning="tab.idleWarning = true"
          @disconnected="tab.status = 'disconnected'"
          @reconnect="reconnect(tab)"
        />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import LucideLock from '~icons/lucide/lock'
import LucidePlus from '~icons/lucide/plus'
import LucideTerminal from '~icons/lucide/terminal'
import LucideX from '~icons/lucide/x'
import { Button } from 'frappe-ui'
import { useAuthStore } from '../stores/auth'
import { serversApi, type Server } from '../api/servers'
import { terminalApi, type SessionCreated } from '../api/terminal'
import TerminalTab from '../components/TerminalTab.vue'

export interface TerminalTabState {
  id: string
  label: string
  serverId: number
  serverName: string
  sshUsername: string
  wsUrl: string
  status: 'connecting' | 'open' | 'disconnected'
  idleWarning: boolean
}

const auth = useAuthStore()
const canAccess = computed(() => auth.hasPermission('terminal:access'))

const servers = ref<Server[]>([])
const loadingServers = ref(false)
const selectedServerId = ref<number | null>(null)
const openingSession = ref(false)
const sessionError = ref('')

const tabs = ref<TerminalTabState[]>([])
const activeTabId = ref<string | null>(null)

let _tabCounter = 0

onMounted(async () => {
  if (!canAccess.value) return
  loadingServers.value = true
  try {
    servers.value = await serversApi.list()
  } finally {
    loadingServers.value = false
  }
})

async function openSession() {
  if (selectedServerId.value == null) return
  sessionError.value = ''
  openingSession.value = true
  try {
    const info: SessionCreated = await terminalApi.createSession(selectedServerId.value)
    const id = `tab-${++_tabCounter}`
    const tab: TerminalTabState = {
      id,
      label: `${info.ssh_username}@${info.server_name}`,
      serverId: selectedServerId.value,
      serverName: info.server_name,
      sshUsername: info.ssh_username,
      wsUrl: terminalApi.wsUrl(info.ticket),
      status: 'connecting',
      idleWarning: false,
    }
    tabs.value.push(tab)
    activeTabId.value = id
  } catch (err: unknown) {
    sessionError.value = err instanceof Error ? err.message : 'Failed to open session'
  } finally {
    openingSession.value = false
  }
}

async function reconnect(tab: TerminalTabState) {
  tab.status = 'connecting'
  tab.idleWarning = false
  try {
    const info: SessionCreated = await terminalApi.createSession(tab.serverId)
    tab.wsUrl = terminalApi.wsUrl(info.ticket)
    tab.label = `${info.ssh_username}@${info.server_name}`
    tab.sshUsername = info.ssh_username
    // TerminalTab watches wsUrl change to reconnect.
  } catch (err: unknown) {
    tab.status = 'disconnected'
    sessionError.value = err instanceof Error ? err.message : 'Reconnect failed'
  }
}

function closeTab(index: number) {
  const tab = tabs.value[index]
  if (!tab) return
  tabs.value.splice(index, 1)
  if (activeTabId.value === tab.id) {
    activeTabId.value = tabs.value[Math.max(0, index - 1)]?.id ?? tabs.value[0]?.id ?? null
  }
}
</script>
