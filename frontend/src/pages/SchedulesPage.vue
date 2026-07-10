<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between border-b border-line px-8 py-5">
      <div>
        <h1 class="text-lg font-semibold text-ink-1">Schedules</h1>
        <p class="text-meta text-ink-2">
          Recurring backups and retention sweeps that run automatically as tracked jobs.
        </p>
      </div>
      <Button
        v-if="canManage"
        variant="solid"
        theme="gray"
        label="New schedule"
        @click="sheetOpen = true"
      >
        <template #prefix><LucideCalendarClock class="h-4 w-4" /></template>
      </Button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-8">
      <p v-if="loadError" class="mb-3 text-label text-err" role="alert">{{ loadError }}</p>

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

      <div v-else class="overflow-hidden rounded-lg border border-line bg-surface">
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
    </div>

    <CreateScheduleSheet :open="sheetOpen" @close="sheetOpen = false" @saved="reload" />
  </div>
</template>

<script setup lang="ts">
import { Button } from 'frappe-ui'
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import LucideCalendarClock from '~icons/lucide/calendar-clock'
import { ApiError } from '../api/client'
import { cadenceLabel, schedulesApi, type Schedule } from '../api/schedules'
import CreateScheduleSheet from '../components/CreateScheduleSheet.vue'
import EmptyState from '../components/EmptyState.vue'
import StatusDot from '../components/StatusDot.vue'
import type { Status } from '../components/types'
import { absoluteTime, relativeTime } from '../lib/servers'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const canManage = computed(() => auth.hasPermission('schedule:manage'))
// Run-now needs the underlying action's permission (backups use backup:create).
const canRun = computed(() => auth.hasPermission('backup:create'))

const schedules = ref<Schedule[]>([])
const loading = ref(true)
const loadError = ref('')
const busyId = ref<number | null>(null)
const sheetOpen = ref(false)

function actionLabel(action: string): string {
  if (action === 'site.backup') return 'Backup'
  if (action === 'backup.retention_sweep') return 'Retention sweep'
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
    schedules.value = await schedulesApi.list()
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

onMounted(reload)
</script>
