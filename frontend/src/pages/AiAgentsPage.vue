<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">AI Agents</h1>
        <p class="text-meta text-ink-2">
          Scoped AI CLI sessions with a pre-change backup and a diff you apply or roll back.
        </p>
      </div>
      <Button
        v-if="canManage"
        variant="solid"
        theme="gray"
        label="Register agent"
        @click="openCreate"
      >
        <template #prefix><LucidePlus class="h-4 w-4" /></template>
      </Button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-4 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Agent cards -->
      <section>
        <div v-if="loading" class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          <div
            v-for="i in 3"
            :key="i"
            class="h-40 animate-pulse rounded-lg border border-line bg-surface"
          />
        </div>

        <EmptyState
          v-else-if="agents.length === 0"
          :icon="LucideBot"
          title="No agents registered"
          :message="
            canManage
              ? 'Register a Claude Code or custom CLI agent, scoped to a directory and allowed servers.'
              : 'No AI agents have been registered yet.'
          "
          :cta-label="canManage ? 'Register agent' : undefined"
          @cta="openCreate"
        />

        <div v-else class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          <AgentCard
            v-for="agent in agents"
            :key="agent.id"
            :agent="agent"
            :can-manage="canManage"
            :can-operate="canOperate"
            @start="startSession"
            @edit="openEdit"
            @delete="askDelete"
          />
        </div>
      </section>

      <!-- Recent sessions -->
      <section v-if="!loading" class="mt-10">
        <h2 class="mb-3 text-section font-semibold text-ink-1">Recent sessions</h2>

        <div
          v-if="sessions.length === 0"
          class="rounded-lg border border-line bg-surface px-4 py-6 text-center text-label text-ink-3"
        >
          No sessions yet. Start one from an agent above.
        </div>

        <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
          <table class="w-full text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">Status</th>
                <th class="px-4 py-2 font-medium">Agent</th>
                <th class="px-4 py-2 font-medium">Server</th>
                <th class="px-4 py-2 font-medium">Directory</th>
                <th class="px-4 py-2 font-medium">Started</th>
                <th class="px-4 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="s in sessions" :key="s.id">
                <td class="px-4 py-2.5">
                  <StatusBadge
                    :status="sessionStatusDot(s.status)"
                    :label="SESSION_STATUS_LABEL[s.status]"
                  />
                </td>
                <td class="px-4 py-2.5 text-ink-1">{{ s.agent_name ?? '—' }}</td>
                <td class="px-4 py-2.5 text-ink-2">
                  {{ s.server_name ?? `server #${s.server_id}` }}
                </td>
                <td class="px-4 py-2.5 font-mono text-meta text-ink-3" :title="s.working_dir">
                  {{ s.working_dir }}
                </td>
                <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(s.started_at)">
                  {{ relativeTime(s.started_at) }}
                </td>
                <td class="px-4 py-2.5">
                  <div class="flex items-center justify-end gap-1">
                    <Button
                      v-if="canOperate && isReviewable(s.status)"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Review"
                      @click="openSession(s)"
                    />
                    <Button
                      v-else-if="canOperate && (s.status === 'starting' || s.status === 'ready')"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Open"
                      @click="openSession(s)"
                    />
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <RegisterAgentSheet
      :open="sheetOpen"
      :agent="editing"
      @close="sheetOpen = false"
      @saved="onSaved"
    />

    <ConfirmModal
      v-model="confirmDelete"
      variant="destructive"
      title="Delete this agent"
      :verb="deleteTarget ? `Delete ${deleteTarget.name}` : 'Delete'"
      :target-name="deleteTarget?.name"
      :loading="deleting"
      :consequences="[
        'Removes the agent configuration.',
        'Past sessions and their diffs are kept for audit.',
      ]"
      @confirm="doDelete"
    />

    <!-- Server picker (only when an agent allows more than one server) -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0"
        leave-active-class="transition duration-150 ease-out"
        leave-to-class="opacity-0"
      >
        <div
          v-if="pickerAgent"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          @click.self="pickerAgent = null"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Choose a server"
            class="w-full max-w-md rounded-lg border border-line bg-raised"
          >
            <div class="border-b border-line px-5 py-4">
              <h2 class="text-section font-semibold text-ink-1">Start on which server?</h2>
              <p class="mt-0.5 text-label text-ink-2">
                {{ pickerAgent.name }} is allowed on more than one server.
              </p>
            </div>
            <div class="max-h-[50vh] space-y-1 overflow-y-auto p-3">
              <button
                v-for="s in pickerServers"
                :key="s.id"
                type="button"
                class="fdm-focus flex w-full items-center gap-2 rounded border border-line bg-surface px-3 py-2 text-left text-label text-ink-1 transition hover:border-line-strong"
                :disabled="starting"
                @click="beginSession(pickerAgent, s.id)"
              >
                <LucideServer class="h-4 w-4 shrink-0 text-ink-3" />
                <span class="truncate">{{ s.name }}</span>
                <span class="truncate text-meta text-ink-3">{{ s.hostname }}</span>
              </button>
            </div>
            <div class="flex justify-end gap-2 border-t border-line px-5 py-3.5">
              <Button variant="subtle" theme="gray" label="Cancel" @click="pickerAgent = null" />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <AgentSessionPanel
      v-if="activeSession"
      :session="activeSession"
      @close="closeSession"
      @changed="reloadSessions"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import LucideBot from '~icons/lucide/bot'
import LucidePlus from '~icons/lucide/plus'
import LucideServer from '~icons/lucide/server'
import { aiAgentsApi, type AgentConfig, type Session } from '../api/aiAgents'
import { ApiError } from '../api/client'
import { serversApi, type Server } from '../api/servers'
import AgentCard from '../components/AgentCard.vue'
import AgentSessionPanel from '../components/AgentSessionPanel.vue'
import ConfirmModal from '../components/ConfirmModal.vue'
import EmptyState from '../components/EmptyState.vue'
import RegisterAgentSheet from '../components/RegisterAgentSheet.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { isReviewable, SESSION_STATUS_LABEL, sessionStatusDot } from '../lib/aiAgents'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canManage = computed(() => auth.hasPermission('ai:manage'))
const canOperate = computed(() => auth.hasPermission('ai:operate'))

const agents = ref<AgentConfig[]>([])
const sessions = ref<Session[]>([])
const servers = ref<Server[]>([])
const loading = ref(true)
const loadError = ref('')

const pickerAgent = ref<AgentConfig | null>(null)
const starting = ref(false)
const pickerServers = computed(() =>
  pickerAgent.value
    ? servers.value.filter((s) => pickerAgent.value!.allowed_server_ids.includes(s.id))
    : [],
)

const sheetOpen = ref(false)
const editing = ref<AgentConfig | null>(null)

const confirmDelete = ref(false)
const deleteTarget = ref<AgentConfig | null>(null)
const deleting = ref(false)

const activeSession = ref<Session | null>(null)

async function reloadAgents() {
  try {
    agents.value = await aiAgentsApi.list()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not load agents.'
  }
}

async function reloadSessions() {
  try {
    sessions.value = await aiAgentsApi.listSessions()
  } catch {
    // Non-fatal: the agents list is the primary content.
  }
}

async function reloadAll() {
  loadError.value = ''
  await Promise.all([reloadAgents(), reloadSessions(), loadServers()])
  loading.value = false
}

async function loadServers() {
  try {
    servers.value = await serversApi.list()
  } catch {
    servers.value = []
  }
}

function openCreate() {
  editing.value = null
  sheetOpen.value = true
}

function openEdit(agent: AgentConfig) {
  editing.value = agent
  sheetOpen.value = true
}

async function onSaved() {
  await reloadAgents()
}

function askDelete(agent: AgentConfig) {
  deleteTarget.value = agent
  confirmDelete.value = true
}

async function doDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  loadError.value = ''
  try {
    await aiAgentsApi.remove(deleteTarget.value.id)
    agents.value = agents.value.filter((a) => a.id !== deleteTarget.value!.id)
    confirmDelete.value = false
    deleteTarget.value = null
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not delete the agent.'
  } finally {
    deleting.value = false
  }
}

function startSession(agent: AgentConfig) {
  loadError.value = ''
  if (agent.allowed_server_ids.length === 0) return
  if (agent.allowed_server_ids.length === 1) {
    beginSession(agent, agent.allowed_server_ids[0])
    return
  }
  // More than one allowed server — let the operator choose.
  pickerAgent.value = agent
}

async function beginSession(agent: AgentConfig, serverId: number) {
  starting.value = true
  loadError.value = ''
  try {
    const session = await aiAgentsApi.startSession(agent.id, serverId)
    pickerAgent.value = null
    activeSession.value = session
    await reloadSessions()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not start the session.'
  } finally {
    starting.value = false
  }
}

function openSession(s: Session) {
  activeSession.value = s
}

function closeSession() {
  activeSession.value = null
  reloadSessions()
}

onMounted(reloadAll)
</script>
