<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Monitoring</h1>
        <p class="text-meta text-ink-2">
          Alert rules that fire over email or webhook when a metric breaches threshold.
        </p>
      </div>
      <Button
        v-if="canManage"
        variant="solid"
        theme="gray"
        label="New alert rule"
        @click="openCreate"
      >
        <template #prefix><LucidePlus class="h-4 w-4" /></template>
      </Button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8 space-y-8">
      <p v-if="loadError" class="text-label text-err" role="alert">{{ loadError }}</p>

      <!-- Alert rules section -->
      <section>
        <h2 class="mb-3 text-section font-semibold text-ink-1">Alert rules</h2>

        <!-- Loading skeleton -->
        <div v-if="loading" class="overflow-hidden rounded-lg border border-line bg-surface">
          <div
            v-for="i in 3"
            :key="i"
            class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0"
          >
            <div class="h-3.5 w-40 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-20 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-16 animate-pulse rounded bg-raised" />
          </div>
        </div>

        <EmptyState
          v-else-if="rules.length === 0"
          :icon="LucideBell"
          title="No alert rules yet"
          :message="
            canManage
              ? 'Create a rule to be notified when a server metric breaches threshold.'
              : 'No alert rules have been configured yet.'
          "
          :cta-label="canManage ? 'New alert rule' : undefined"
          @cta="openCreate"
        />

        <div v-else class="overflow-x-auto overflow-hidden rounded-lg border border-line bg-surface">
          <table class="w-full min-w-[700px] text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">Name</th>
                <th class="px-4 py-2 font-medium">Condition</th>
                <th class="px-4 py-2 font-medium">Scope</th>
                <th class="px-4 py-2 font-medium">Cooldown</th>
                <th class="px-4 py-2 font-medium">Channels</th>
                <th class="px-4 py-2 font-medium">Enabled</th>
                <th class="px-4 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="r in rules" :key="r.id">
                <td class="px-4 py-2.5 font-medium text-ink-1">{{ r.name }}</td>
                <td class="px-4 py-2.5 font-mono text-ink-2">
                  {{ METRIC_LABELS[r.metric] ?? r.metric }}
                  <span class="text-ink-3">{{ r.comparator }}</span>
                  {{ r.threshold }}
                </td>
                <td class="px-4 py-2.5 text-ink-2">
                  <span v-if="r.scope === 'global'">All servers</span>
                  <span v-else class="text-ink-1">Server #{{ r.scope_server_id }}</span>
                </td>
                <td class="px-4 py-2.5 text-ink-2">{{ r.cooldown_minutes }}m</td>
                <td class="px-4 py-2.5">
                  <div class="flex items-center gap-1.5">
                    <span
                      v-if="r.channel_email"
                      class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-meta bg-raised text-ink-2"
                    >
                      <LucideMail class="h-3 w-3" /> Email
                    </span>
                    <span
                      v-if="r.channel_webhook"
                      class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-meta bg-raised text-ink-2"
                    >
                      <LucideWebhook class="h-3 w-3" /> Webhook
                    </span>
                  </div>
                </td>
                <td class="px-4 py-2.5">
                  <button
                    type="button"
                    role="switch"
                    :aria-checked="r.enabled"
                    :disabled="!canManage || busyId === r.id"
                    class="fdm-focus inline-flex h-5 w-9 items-center rounded-full border transition disabled:opacity-50"
                    :class="r.enabled ? 'border-ok/40 bg-ok/25' : 'border-line bg-raised'"
                    :title="r.enabled ? 'Enabled — click to pause' : 'Disabled — click to enable'"
                    @click="toggleEnabled(r)"
                  >
                    <span
                      class="ml-0.5 h-4 w-4 rounded-full bg-ink-1 transition"
                      :class="r.enabled ? 'translate-x-4' : ''"
                    />
                  </button>
                </td>
                <td class="px-4 py-2.5">
                  <div class="flex items-center justify-end gap-1">
                    <Button
                      v-if="canManage"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Test"
                      :disabled="busyId === r.id"
                      :title="`Fire a synthetic alert on ${r.name} to validate channels`"
                      @click="testRule(r)"
                    />
                    <Button
                      v-if="canManage"
                      variant="subtle"
                      theme="gray"
                      size="sm"
                      label="Edit"
                      :disabled="busyId === r.id"
                      @click="openEdit(r)"
                    />
                    <Button
                      v-if="canManage"
                      variant="subtle"
                      theme="red"
                      size="sm"
                      label="Delete"
                      :disabled="busyId === r.id"
                      @click="deleteRule(r)"
                    />
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <p v-if="actionMessage" class="mt-2 text-meta text-ink-2" role="status">{{ actionMessage }}</p>
      </section>

      <!-- Incidents section -->
      <section>
        <div class="mb-3 flex items-center justify-between">
          <h2 class="text-section font-semibold text-ink-1">Recent incidents</h2>
          <span v-if="incidents.length" class="text-meta text-ink-2">
            Showing {{ incidents.length }} most recent
          </span>
        </div>

        <div v-if="incidentsLoading" class="overflow-hidden rounded-lg border border-line bg-surface">
          <div
            v-for="i in 3"
            :key="i"
            class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0"
          >
            <div class="h-3.5 w-36 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
          </div>
        </div>

        <EmptyState
          v-else-if="incidents.length === 0"
          :icon="LucideCheckCircle"
          title="No incidents"
          message="No alert firings recorded yet. Rules fire when a metric breaches threshold."
        />

        <div v-else class="overflow-x-auto overflow-hidden rounded-lg border border-line bg-surface">
          <table class="w-full min-w-[640px] text-left">
            <thead>
              <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                <th class="px-4 py-2 font-medium">Rule</th>
                <th class="px-4 py-2 font-medium">Server</th>
                <th class="px-4 py-2 font-medium">Condition</th>
                <th class="px-4 py-2 font-medium">Value</th>
                <th class="px-4 py-2 font-medium">Status</th>
                <th class="px-4 py-2 font-medium">Fired</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line text-label">
              <tr v-for="f in incidents" :key="f.id">
                <td class="px-4 py-2.5 font-medium text-ink-1">{{ f.rule_name ?? '(deleted rule)' }}</td>
                <td class="px-4 py-2.5 text-ink-2">{{ f.server_name ?? '—' }}</td>
                <td class="px-4 py-2.5 font-mono text-ink-2">
                  {{ METRIC_LABELS[f.metric as keyof typeof METRIC_LABELS] ?? f.metric }}
                  {{ f.comparator }}
                  {{ f.threshold }}
                </td>
                <td class="px-4 py-2.5 font-mono text-ink-1">
                  {{ f.value != null ? f.value.toFixed(1) : '—' }}
                </td>
                <td class="px-4 py-2.5">
                  <span v-if="f.resolved_at" class="inline-flex items-center gap-1.5 text-ok">
                    <StatusDot status="ok" size="sm" /> Resolved
                  </span>
                  <span v-else class="inline-flex items-center gap-1.5 text-err">
                    <StatusDot status="err" size="sm" /> Open
                  </span>
                </td>
                <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(f.created_at)">
                  {{ relativeTime(f.created_at) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <AlertRuleSheet
    :open="sheetOpen"
    :rule="editingRule"
    @close="sheetOpen = false; editingRule = null"
    @saved="reload"
  />

  <ConfirmModal
    v-model="deleteModal"
    title="Delete alert rule"
    :message="`Delete '${deletingRule?.name}'? This removes all associated incident history.`"
    verb="Delete rule"
    variant="destructive"
    :target-name="deletingRule?.name"
    :loading="busyId !== null"
    @confirm="confirmDelete"
  />
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import LucideBell from '~icons/lucide/bell'
import LucideCheckCircle from '~icons/lucide/check-circle'
import LucideMail from '~icons/lucide/mail'
import LucidePlus from '~icons/lucide/plus'
import LucideWebhook from '~icons/lucide/webhook'
import { ApiError } from '../api/client'
import { alertsApi, METRIC_LABELS, type AlertFiring, type AlertRule } from '../api/alerts'
import AlertRuleSheet from '../components/AlertRuleSheet.vue'
import ConfirmModal from '../components/ConfirmModal.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusDot from '../components/StatusDot.vue'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canManage = computed(() => auth.hasPermission('alert:manage'))

const rules = ref<AlertRule[]>([])
const incidents = ref<AlertFiring[]>([])
const loading = ref(true)
const incidentsLoading = ref(true)
const loadError = ref('')
const actionMessage = ref('')
const busyId = ref<number | null>(null)
const sheetOpen = ref(false)
const editingRule = ref<AlertRule | null>(null)
const deleteModal = ref(false)
const deletingRule = ref<AlertRule | null>(null)

function openCreate() {
  editingRule.value = null
  sheetOpen.value = true
}

function openEdit(rule: AlertRule) {
  editingRule.value = rule
  sheetOpen.value = true
}

async function reload() {
  loadError.value = ''
  loading.value = true
  try {
    rules.value = await alertsApi.list()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not load alert rules.'
  } finally {
    loading.value = false
  }
  await loadIncidents()
}

async function loadIncidents() {
  incidentsLoading.value = true
  try {
    const data = await alertsApi.incidents()
    incidents.value = data.incidents
  } catch {
    incidents.value = []
  } finally {
    incidentsLoading.value = false
  }
}

async function toggleEnabled(rule: AlertRule) {
  if (!canManage.value) return
  busyId.value = rule.id
  actionMessage.value = ''
  try {
    const updated = await alertsApi.update(rule.id, { enabled: !rule.enabled })
    Object.assign(rule, updated)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not update the rule.'
  } finally {
    busyId.value = null
  }
}

async function testRule(rule: AlertRule) {
  if (!canManage.value) return
  busyId.value = rule.id
  actionMessage.value = ''
  try {
    await alertsApi.test(rule.id)
    actionMessage.value = `Test alert sent for "${rule.name}". Check your configured channels.`
    await loadIncidents()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not send test alert.'
  } finally {
    busyId.value = null
  }
}

function deleteRule(rule: AlertRule) {
  deletingRule.value = rule
  deleteModal.value = true
}

async function confirmDelete() {
  if (!deletingRule.value) return
  busyId.value = deletingRule.value.id
  try {
    await alertsApi.remove(deletingRule.value.id)
    rules.value = rules.value.filter((r) => r.id !== deletingRule.value!.id)
    await loadIncidents()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not delete the rule.'
  } finally {
    busyId.value = null
    deleteModal.value = false
    deletingRule.value = null
  }
}

onMounted(reload)
</script>
