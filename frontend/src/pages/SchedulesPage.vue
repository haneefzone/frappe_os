<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Schedules</h1>
        <p class="text-meta text-ink-2">
          Recurring jobs and maintenance windows that shape when dangerous operations are allowed.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <Button
          v-if="canManage"
          variant="subtle"
          theme="gray"
          label="New window"
          @click="windowSheetOpen = true"
        >
          <template #prefix><LucideShieldOff class="h-4 w-4" /></template>
        </Button>
        <Button
          v-if="canManage"
          variant="solid"
          theme="gray"
          label="New schedule"
          @click="sheetOpen = true"
        >
          <template #prefix><LucideCalendarClock class="h-4 w-4" /></template>
        </Button>
      </div>
    </header>

    <!-- View tabs -->
    <div class="flex items-center gap-0 border-b border-line bg-base px-8">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="fdm-focus -mb-px border-b-2 px-4 py-2.5 text-label transition"
        :class="
          activeTab === tab.key
            ? 'border-ink-1 font-medium text-ink-1'
            : 'border-transparent text-ink-3 hover:text-ink-2'
        "
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

      <!-- ================================================================ -->
      <!-- LIST TAB                                                          -->
      <!-- ================================================================ -->
      <template v-if="activeTab === 'list'">
        <div v-if="loading" class="rounded-lg border border-line bg-surface">
          <div
            v-for="i in 3"
            :key="i"
            class="flex items-center gap-6 border-b border-line px-4 py-3 last:border-0"
          >
            <div class="h-3.5 w-48 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-24 animate-pulse rounded bg-raised" />
            <div class="h-3.5 w-20 animate-pulse rounded bg-raised" />
          </div>
        </div>

        <EmptyState
          v-else-if="schedules.length === 0"
          :icon="LucideCalendarClock"
          title="No schedules yet"
          :message="
            canManage
              ? 'Create a schedule to back up a site or prune old backups automatically.'
              : 'No recurring schedules have been created yet.'
          "
          :cta-label="canManage ? 'New schedule' : undefined"
          @cta="sheetOpen = true"
        />

        <div v-else class="space-y-6">
          <!-- Schedules table -->
          <div class="overflow-hidden rounded-lg border border-line bg-surface">
            <table class="w-full text-left">
              <thead>
                <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                  <th class="px-4 py-2 font-medium">Schedule</th>
                  <th class="px-4 py-2 font-medium">Target</th>
                  <th class="px-4 py-2 font-medium">Cadence</th>
                  <th class="px-4 py-2 font-medium">Next run</th>
                  <th class="px-4 py-2 font-medium">Last result</th>
                  <th class="px-4 py-2 font-medium">Enabled</th>
                  <th class="px-4 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-line text-label">
                <tr v-for="s in schedules" :key="s.id">
                  <td class="px-4 py-2.5">
                    <span class="font-medium text-ink-1">{{ s.name }}</span>
                    <span class="block text-meta text-ink-3">{{ actionLabel(s.action_name) }}</span>
                  </td>
                  <td class="px-4 py-2.5 text-ink-2">{{ s.target_label ?? `site #${s.target_id}` }}</td>
                  <td class="px-4 py-2.5 text-ink-2">{{ cadenceLabel(s) }}</td>
                  <td class="px-4 py-2.5 text-ink-3" :title="absoluteTime(s.next_run_at)">
                    {{ s.enabled ? relativeTime(s.next_run_at) : '—' }}
                  </td>
                  <td class="px-4 py-2.5">
                    <span v-if="s.last_run_job_id" class="inline-flex items-center gap-2">
                      <StatusDot :status="statusDot(s.last_run_status)" />
                      <RouterLink
                        :to="`/jobs/${s.last_run_job_id}`"
                        class="fdm-focus text-ink-2 hover:text-ink-1"
                        :title="absoluteTime(s.last_run_at)"
                      >
                        {{ relativeTime(s.last_run_at) }}
                      </RouterLink>
                    </span>
                    <span v-else class="text-ink-3">Never run</span>
                  </td>
                  <td class="px-4 py-2.5">
                    <button
                      type="button"
                      role="switch"
                      :aria-checked="s.enabled"
                      :disabled="!canManage || busyId === s.id"
                      class="fdm-focus inline-flex h-5 w-9 items-center rounded-full border transition disabled:opacity-50"
                      :class="s.enabled ? 'border-ok/40 bg-ok/25' : 'border-line bg-raised'"
                      :title="s.enabled ? 'Enabled — click to pause' : 'Disabled — click to enable'"
                      @click="toggle(s)"
                    >
                      <span
                        class="ml-0.5 h-4 w-4 rounded-full bg-ink-1 transition"
                        :class="s.enabled ? 'translate-x-4' : ''"
                      />
                    </button>
                  </td>
                  <td class="px-4 py-2.5">
                    <div class="flex items-center justify-end gap-1">
                      <Button
                        v-if="canRun"
                        variant="subtle"
                        theme="gray"
                        size="sm"
                        label="Run now"
                        :disabled="busyId === s.id"
                        @click="runNow(s)"
                      />
                      <Button
                        v-if="canManage"
                        variant="subtle"
                        theme="gray"
                        size="sm"
                        label="Delete"
                        :disabled="busyId === s.id"
                        @click="remove(s)"
                      />
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Maintenance windows list -->
          <div>
            <div class="mb-3 flex items-center justify-between">
              <h2 class="text-label font-semibold text-ink-1">Maintenance windows</h2>
              <Button
                v-if="canManage"
                variant="subtle"
                theme="gray"
                size="sm"
                label="New window"
                @click="windowSheetOpen = true"
              />
            </div>
            <EmptyState
              v-if="maintenanceWindows.length === 0"
              :icon="LucideShieldOff"
              title="No maintenance windows"
              message="Maintenance windows block dangerous operations (updates, restores, production setup) during recurring time slots."
              :cta-label="canManage ? 'New window' : undefined"
              @cta="windowSheetOpen = true"
            />
            <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
              <table class="w-full text-left">
                <thead>
                  <tr class="border-b border-line text-meta uppercase tracking-wide text-ink-3">
                    <th class="px-4 py-2 font-medium">Window</th>
                    <th class="px-4 py-2 font-medium">Server</th>
                    <th class="px-4 py-2 font-medium">Schedule</th>
                    <th class="px-4 py-2 font-medium">Blocks</th>
                    <th class="px-4 py-2 font-medium">Status</th>
                    <th class="px-4 py-2 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-line text-label">
                  <tr v-for="mw in maintenanceWindows" :key="mw.id">
                    <td class="px-4 py-2.5 font-medium text-ink-1">{{ mw.name }}</td>
                    <td class="px-4 py-2.5 text-ink-2">{{ serverNameFor(mw.server_id) }}</td>
                    <td class="px-4 py-2.5 text-ink-2">
                      {{ mw.cron }}
                      <span class="text-meta text-ink-3"> · {{ mw.duration_minutes }}m · {{ mw.timezone }}</span>
                    </td>
                    <td class="px-4 py-2.5">
                      <span
                        v-for="cls in mw.blocked_danger_classes"
                        :key="cls"
                        class="mr-1 inline-flex items-center rounded bg-err/10 px-1.5 py-0.5 text-meta text-err"
                      >
                        {{ DANGER_CLASS_LABELS[cls as DangerClass] ?? cls }}
                      </span>
                      <span v-if="mw.blocked_danger_classes.length === 0" class="text-ink-4">
                        None
                      </span>
                    </td>
                    <td class="px-4 py-2.5">
                      <span
                        v-if="mw.is_active_now"
                        class="inline-flex items-center gap-1 text-err"
                      >
                        <span class="h-2 w-2 rounded-full bg-err" /> Active now
                      </span>
                      <span v-else-if="mw.enabled" class="text-ink-3">Scheduled</span>
                      <span v-else class="text-ink-4">Disabled</span>
                    </td>
                    <td class="px-4 py-2.5">
                      <div class="flex items-center justify-end gap-1">
                        <Button
                          v-if="canManage"
                          variant="subtle"
                          theme="gray"
                          size="sm"
                          label="Edit"
                          @click="editWindow(mw)"
                        />
                        <Button
                          v-if="canManage"
                          variant="subtle"
                          theme="gray"
                          size="sm"
                          label="Delete"
                          @click="removeWindow(mw)"
                        />
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </template>

      <!-- ================================================================ -->
      <!-- CALENDAR TAB                                                      -->
      <!-- ================================================================ -->
      <template v-else>
        <div v-if="loading" class="space-y-4">
          <div v-for="i in 2" :key="i" class="h-32 animate-pulse rounded-lg bg-raised" />
        </div>
        <SchedulesCalendar
          v-else
          :schedules="schedules"
          :maintenance-windows="maintenanceWindows"
          :server-names="serverNamesMap"
        />
      </template>
    </div>

    <CreateScheduleSheet
      :open="sheetOpen"
      :initial-name="sheetInitialName"
      @close="sheetOpen = false"
      @saved="reload"
    />

    <CreateMaintenanceWindowSheet
      :open="windowSheetOpen"
      :servers="servers"
      :editing="editingWindow"
      @close="closeWindowSheet"
      @saved="onWindowSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import LucideCalendarClock from '~icons/lucide/calendar-clock'
import LucideShieldOff from '~icons/lucide/shield-off'
import { ApiError } from '../api/client'
import {
  DANGER_CLASS_LABELS,
  maintenanceWindowsApi,
  type DangerClass,
  type MaintenanceWindow,
} from '../api/maintenanceWindows'
import { cadenceLabel, schedulesApi, type Schedule } from '../api/schedules'
import { serversApi, type Server } from '../api/servers'
import CreateMaintenanceWindowSheet from '../components/CreateMaintenanceWindowSheet.vue'
import CreateScheduleSheet from '../components/CreateScheduleSheet.vue'
import EmptyState from '../components/EmptyState.vue'
import SchedulesCalendar from '../components/SchedulesCalendar.vue'
import StatusDot from '../components/StatusDot.vue'
import type { Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const auth = useAuthStore()
const canManage = computed(() => auth.hasPermission('schedule:manage'))
const canRun = computed(() => auth.hasPermission('backup:create'))

const schedules = ref<Schedule[]>([])
const maintenanceWindows = ref<MaintenanceWindow[]>([])
const servers = ref<Server[]>([])
const loading = ref(true)
const loadError = ref('')
const busyId = ref<number | null>(null)
const sheetOpen = ref(false)
const sheetInitialName = ref('')
const windowSheetOpen = ref(false)
const editingWindow = ref<MaintenanceWindow | null>(null)

const tabs = [
  { key: 'list', label: 'List' },
  { key: 'calendar', label: 'Calendar' },
] as const
type TabKey = (typeof tabs)[number]['key']
const activeTab = ref<TabKey>('list')

const serverNamesMap = computed<Record<number, string>>(() => {
  const map: Record<number, string> = {}
  for (const s of servers.value) map[s.id] = s.name
  return map
})

function serverNameFor(serverId: number): string {
  return serverNamesMap.value[serverId] ?? `server #${serverId}`
}

function actionLabel(action: string): string {
  if (action === 'site.backup') return 'Backup'
  if (action === 'backup.retention_sweep') return 'Retention sweep'
  if (action === 'report.generate') return 'Report delivery'
  if (action === 'backup.restore_test') return 'Restore test'
  return action
}

function statusDot(status: string | null): Status {
  switch (status) {
    case 'success':
      return 'ok'
    case 'failure':
      return 'err'
    case 'running':
    case 'pending':
      return 'running'
    default:
      return 'muted'
  }
}

async function reload() {
  loadError.value = ''
  try {
    const [s, mw, srv] = await Promise.all([
      schedulesApi.list(),
      maintenanceWindowsApi.list(),
      serversApi.list(),
    ])
    schedules.value = s
    maintenanceWindows.value = mw
    servers.value = srv
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not load schedules.'
  } finally {
    loading.value = false
  }
}

async function toggle(s: Schedule) {
  if (!canManage.value) return
  busyId.value = s.id
  try {
    const updated = await schedulesApi.setEnabled(s.id, !s.enabled)
    Object.assign(s, updated)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not update the schedule.'
  } finally {
    busyId.value = null
  }
}

async function runNow(s: Schedule) {
  busyId.value = s.id
  loadError.value = ''
  try {
    await schedulesApi.runNow(s.id)
    await reload()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not run the schedule.'
  } finally {
    busyId.value = null
  }
}

async function remove(s: Schedule) {
  if (!canManage.value) return
  busyId.value = s.id
  try {
    await schedulesApi.remove(s.id)
    schedules.value = schedules.value.filter((x) => x.id !== s.id)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not delete the schedule.'
  } finally {
    busyId.value = null
  }
}

function editWindow(mw: MaintenanceWindow) {
  editingWindow.value = mw
  windowSheetOpen.value = true
}

function closeWindowSheet() {
  windowSheetOpen.value = false
  editingWindow.value = null
}

function onWindowSaved(saved: MaintenanceWindow) {
  const idx = maintenanceWindows.value.findIndex((w) => w.id === saved.id)
  if (idx >= 0) maintenanceWindows.value[idx] = saved
  else maintenanceWindows.value.push(saved)
}

async function removeWindow(mw: MaintenanceWindow) {
  try {
    await maintenanceWindowsApi.remove(mw.id)
    maintenanceWindows.value = maintenanceWindows.value.filter((w) => w.id !== mw.id)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Could not delete the maintenance window.'
  }
}

onMounted(() => {
  void reload()
  // When navigated from /reports?report=<id>&name=<title>, auto-open the sheet.
  const name = route.query.name
  if (route.query.report && canManage.value && name) {
    sheetInitialName.value = String(name)
    sheetOpen.value = true
  }
})
</script>
