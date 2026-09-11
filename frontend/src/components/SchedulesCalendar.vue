<template>
  <div class="select-none">
    <!-- Controls bar -->
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-1">
        <button
          type="button"
          class="fdm-focus rounded p-1 text-ink-2 transition hover:bg-raised hover:text-ink-1"
          aria-label="Previous"
          @click="prev"
        >
          <LucideChevronLeft class="h-4 w-4" />
        </button>
        <span class="min-w-[160px] text-center text-label font-semibold text-ink-1">
          {{ headerLabel }}
        </span>
        <button
          type="button"
          class="fdm-focus rounded p-1 text-ink-2 transition hover:bg-raised hover:text-ink-1"
          aria-label="Next"
          @click="next"
        >
          <LucideChevronRight class="h-4 w-4" />
        </button>
        <button
          type="button"
          class="fdm-focus ml-1 rounded px-2 py-0.5 text-meta text-ink-2 transition hover:bg-raised hover:text-ink-1"
          @click="goToday"
        >
          Today
        </button>
      </div>

      <div class="flex items-center gap-1 rounded-lg border border-line bg-surface p-0.5">
        <button
          type="button"
          class="fdm-focus rounded px-3 py-1 text-label transition"
          :class="mode === 'month' ? 'bg-raised font-medium text-ink-1' : 'text-ink-2 hover:text-ink-1'"
          @click="mode = 'month'"
        >
          Month
        </button>
        <button
          type="button"
          class="fdm-focus rounded px-3 py-1 text-label transition"
          :class="mode === 'week' ? 'bg-raised font-medium text-ink-1' : 'text-ink-2 hover:text-ink-1'"
          @click="mode = 'week'"
        >
          Week
        </button>
      </div>
    </div>

    <!-- Legend -->
    <div class="mb-4 flex flex-wrap items-center gap-4 text-meta text-ink-2">
      <span class="flex items-center gap-1.5">
        <span class="h-2.5 w-2.5 rounded-full bg-ok/70" />
        Backup schedule
      </span>
      <span class="flex items-center gap-1.5">
        <span class="h-2.5 w-2.5 rounded-full bg-info/70" />
        Restore test
      </span>
      <span class="flex items-center gap-1.5">
        <span class="h-2.5 w-2.5 rounded-full bg-warn/70" />
        Update window
      </span>
      <span class="flex items-center gap-1.5">
        <span class="h-2.5 w-2.5 rounded-full bg-err/30" />
        Maintenance window
      </span>
      <span class="flex items-center gap-1.5">
        <LucideAlertTriangle class="h-3 w-3 text-err" />
        Conflict
      </span>
    </div>

    <!-- Month view -->
    <div v-if="mode === 'month'" class="overflow-hidden rounded-lg border border-line">
      <div class="grid grid-cols-7 border-b border-line bg-raised">
        <div
          v-for="d in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']"
          :key="d"
          class="py-2 text-center text-meta font-medium uppercase tracking-wide text-ink-3"
        >
          {{ d }}
        </div>
      </div>
      <div class="grid grid-cols-7 bg-surface">
        <div
          v-for="cell in monthCells"
          :key="cell.key"
          class="relative min-h-[100px] border-b border-r border-line p-1.5 last:border-r-0"
          :class="{
            'bg-base': !cell.currentMonth,
            'ring-1 ring-inset ring-ink-1/20': cell.isToday,
          }"
        >
          <span
            class="mb-1 block text-right text-meta"
            :class="cell.currentMonth ? 'text-ink-2' : 'text-ink-4'"
          >
            {{ cell.dayNum }}
          </span>

          <!-- Maintenance window shading -->
          <div
            v-if="hasMaintenance(cell.date)"
            class="absolute inset-0 bg-err/5 pointer-events-none"
          />

          <!-- Events -->
          <div class="relative space-y-0.5">
            <CalendarEvent
              v-for="(ev, i) in cell.events.slice(0, maxEventsPerCell)"
              :key="i"
              :event="ev"
              @click="selectedEvent = ev"
            />
            <button
              v-if="cell.events.length > maxEventsPerCell"
              type="button"
              class="fdm-focus block text-left text-meta text-ink-3 hover:text-ink-1"
              @click="expandDay = cell.date"
            >
              +{{ cell.events.length - maxEventsPerCell }} more
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Week view -->
    <div v-else class="overflow-hidden rounded-lg border border-line">
      <div class="grid grid-cols-7 border-b border-line bg-raised">
        <div
          v-for="day in weekDays"
          :key="day.key"
          class="py-2 text-center text-meta font-medium"
          :class="day.isToday ? 'text-ink-1' : 'text-ink-3'"
        >
          <div class="uppercase tracking-wide">{{ day.label }}</div>
          <div
            class="mx-auto mt-0.5 flex h-7 w-7 items-center justify-center rounded-full text-label"
            :class="day.isToday ? 'bg-ink-1 text-base font-semibold' : ''"
          >
            {{ day.dayNum }}
          </div>
        </div>
      </div>
      <div class="grid min-h-[200px] grid-cols-7 bg-surface">
        <div
          v-for="day in weekDays"
          :key="day.key + '_body'"
          class="border-r border-line p-1.5 last:border-r-0"
          :class="{ 'bg-err/5': hasMaintenance(day.date) }"
        >
          <div class="space-y-0.5">
            <CalendarEvent
              v-for="(ev, i) in day.events"
              :key="i"
              :event="ev"
              @click="selectedEvent = ev"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- Conflict warning banner -->
    <div
      v-if="conflicts.length > 0"
      class="mt-4 flex items-start gap-2 rounded-lg border border-err/30 bg-err/8 px-4 py-3"
      role="alert"
    >
      <LucideAlertTriangle class="mt-0.5 h-4 w-4 flex-shrink-0 text-err" />
      <div class="text-label text-ink-1">
        <p class="font-medium">{{ conflicts.length }} scheduling conflict{{ conflicts.length > 1 ? 's' : '' }} detected</p>
        <p class="mt-0.5 text-meta text-ink-2">
          Dangerous jobs overlap maintenance windows — they will be refused when the window is
          active. Review the highlighted dates.
        </p>
      </div>
    </div>

    <!-- Event detail popover -->
    <Teleport to="body">
      <div
        v-if="selectedEvent"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
        @click.self="selectedEvent = null"
      >
        <div class="w-80 rounded-xl border border-line bg-base p-5 shadow-2xl">
          <div class="mb-3 flex items-start justify-between">
            <div class="flex items-center gap-2">
              <span
                class="mt-0.5 h-2.5 w-2.5 flex-shrink-0 rounded-full"
                :class="eventColor(selectedEvent)"
              />
              <span class="text-label font-semibold text-ink-1">{{ selectedEvent.title }}</span>
            </div>
            <button
              type="button"
              class="fdm-focus rounded p-1 text-ink-3 hover:text-ink-1"
              @click="selectedEvent = null"
            >
              <LucideX class="h-4 w-4" />
            </button>
          </div>
          <dl class="space-y-1 text-meta">
            <div class="flex gap-2">
              <dt class="w-24 text-ink-3">Type</dt>
              <dd class="text-ink-1">{{ selectedEvent.typeLabel }}</dd>
            </div>
            <div v-if="selectedEvent.subtitle" class="flex gap-2">
              <dt class="w-24 text-ink-3">Target</dt>
              <dd class="text-ink-1">{{ selectedEvent.subtitle }}</dd>
            </div>
            <div class="flex gap-2">
              <dt class="w-24 text-ink-3">Time</dt>
              <dd class="text-ink-1">{{ formatEventTime(selectedEvent) }}</dd>
            </div>
            <div v-if="selectedEvent.duration" class="flex gap-2">
              <dt class="w-24 text-ink-3">Duration</dt>
              <dd class="text-ink-1">{{ selectedEvent.duration }}</dd>
            </div>
            <div v-if="selectedEvent.conflict" class="flex gap-2 text-err">
              <LucideAlertTriangle class="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
              <span>Conflicts with maintenance window — will be refused when active.</span>
            </div>
          </dl>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, ref } from 'vue'
import LucideAlertTriangle from '~icons/lucide/alert-triangle'
import LucideChevronLeft from '~icons/lucide/chevron-left'
import LucideChevronRight from '~icons/lucide/chevron-right'
import LucideX from '~icons/lucide/x'
import type { MaintenanceWindow } from '../api/maintenanceWindows'
import type { Schedule } from '../api/schedules'

// --------------------------------------------------------------------------- //
// Props
// --------------------------------------------------------------------------- //

const props = defineProps<{
  schedules: Schedule[]
  maintenanceWindows: MaintenanceWindow[]
  serverNames?: Record<number, string>
}>()

// --------------------------------------------------------------------------- //
// Calendar state
// --------------------------------------------------------------------------- //

type CalView = 'month' | 'week'
const mode = ref<CalView>('month')
const today = new Date()
const cursor = ref(new Date(today.getFullYear(), today.getMonth(), 1))
const expandDay = ref<Date | null>(null)
const selectedEvent = ref<CalendarEvent | null>(null)
const maxEventsPerCell = 3

// --------------------------------------------------------------------------- //
// Event model
// --------------------------------------------------------------------------- //

interface CalendarEvent {
  title: string
  typeLabel: string
  subtitle?: string
  date: Date
  kind: 'backup' | 'restore_test' | 'update' | 'maintenance'
  duration?: string
  conflict: boolean
}

// --------------------------------------------------------------------------- //
// Navigation
// --------------------------------------------------------------------------- //

function prev() {
  if (mode.value === 'month') {
    cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() - 1, 1)
  } else {
    cursor.value = new Date(cursor.value.getTime() - 7 * 86400_000)
  }
}

function next() {
  if (mode.value === 'month') {
    cursor.value = new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 1)
  } else {
    cursor.value = new Date(cursor.value.getTime() + 7 * 86400_000)
  }
}

function goToday() {
  cursor.value =
    mode.value === 'month' ? new Date(today.getFullYear(), today.getMonth(), 1) : mondayOf(today)
}

function mondayOf(d: Date): Date {
  const day = d.getDay() || 7
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - (day - 1))
}

// --------------------------------------------------------------------------- //
// Header label
// --------------------------------------------------------------------------- //

const headerLabel = computed(() => {
  if (mode.value === 'month') {
    return cursor.value.toLocaleDateString(undefined, { year: 'numeric', month: 'long' })
  }
  const mon = mondayOfCursor.value
  const sun = new Date(mon.getTime() + 6 * 86400_000)
  if (mon.getMonth() === sun.getMonth()) {
    return mon.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
  }
  return (
    mon.toLocaleDateString(undefined, { month: 'short' }) +
    ' – ' +
    sun.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
  )
})

const mondayOfCursor = computed(() => mondayOf(cursor.value))

// --------------------------------------------------------------------------- //
// Event generation from schedules
// --------------------------------------------------------------------------- //

function actionKind(action: string): CalendarEvent['kind'] {
  if (action.includes('backup')) return 'backup'
  if (action.includes('restore_test')) return 'restore_test'
  if (action.includes('update') || action.includes('clone') || action.includes('promote'))
    return 'update'
  return 'backup'
}

function actionTypeLabel(action: string): string {
  if (action === 'site.backup' || action === 'site.backup_db') return 'Backup'
  if (action === 'backup.retention_sweep') return 'Retention sweep'
  if (action === 'backup.restore_test') return 'Restore test'
  if (action === 'bench.update') return 'Bench update'
  if (action === 'site.clone_to_staging') return 'Clone to staging'
  if (action === 'site.promote_update') return 'Promote update'
  return action
}

function nextOccurrences(s: Schedule, start: Date, end: Date): Date[] {
  if (!s.enabled) return []
  const result: Date[] = []
  if (s.interval_seconds) {
    const firstMs =
      s.next_run_at ? Math.max(new Date(s.next_run_at).getTime(), start.getTime()) : start.getTime()
    let t = firstMs
    while (t <= end.getTime() && result.length < 60) {
      result.push(new Date(t))
      t += s.interval_seconds * 1000
    }
    return result
  }
  // For cron schedules we approximate: use next_run_at as a hint and show it
  // once in the visible range (a full cron expansion would need a server call).
  if (s.next_run_at) {
    const d = new Date(s.next_run_at)
    if (d >= start && d <= end) result.push(d)
  }
  return result
}

// --------------------------------------------------------------------------- //
// Maintenance window occurrence generation (approximation for calendar display)
// --------------------------------------------------------------------------- //

function maintenanceDays(start: Date, end: Date): Set<string> {
  const days = new Set<string>()
  for (const mw of props.maintenanceWindows) {
    if (!mw.enabled) continue
    // Approximate: mark any day that has a cron trigger plus duration coverage.
    // We do the same simple next_run_at / interval heuristic — a real croniter
    // expansion runs server-side via the API (is_active_now).
    if (mw.is_active_now) {
      days.add(dateKey(new Date()))
    }
    // Walk day-by-day and mark if any cron trigger falls within the window on that day.
    // We can't run croniter in the browser, so we rely on a weekly pattern heuristic:
    // if the window fires on this day of week (parsed from a simple "N N * * DOW" cron),
    // mark it. For complex crons we just mark is_active_now.
    const dowMatch = parseCronDow(mw.cron)
    if (dowMatch) {
      const d = new Date(start)
      while (d <= end) {
        // getDay() returns 0=Sunday, ISO 1=Monday..7=Sunday; cron dow 1=Monday
        const isoDow = d.getDay() || 7
        if (dowMatch.has(isoDow)) {
          days.add(dateKey(d))
        }
        d.setDate(d.getDate() + 1)
      }
    }
  }
  return days
}

/** Parse day-of-week from "MIN HOUR * * DOW" shaped cron expressions (covers most use cases). */
function parseCronDow(cron: string): Set<number> | null {
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return null
  const dowPart = parts[4]
  if (dowPart === '*') return null
  const days = new Set<number>()
  for (const seg of dowPart.split(',')) {
    if (seg.includes('-')) {
      const [from, to] = seg.split('-').map(Number)
      for (let d = from; d <= to; d++) days.add(d === 0 ? 7 : d) // 0→7 = Sunday
    } else {
      const n = Number(seg)
      days.add(n === 0 ? 7 : n)
    }
  }
  return days.size > 0 ? days : null
}

function hasMaintenance(date: Date): boolean {
  return _maintenanceDaysCache.value.has(dateKey(date))
}

const _maintenanceDaysCache = computed(() => {
  const start = mode.value === 'month' ? monthStart.value : mondayOfCursor.value
  const end =
    mode.value === 'month'
      ? new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 0)
      : new Date(mondayOfCursor.value.getTime() + 6 * 86400_000)
  return maintenanceDays(start, end)
})

// --------------------------------------------------------------------------- //
// Conflict detection
// --------------------------------------------------------------------------- //

/** Events on days that are also maintenance-window days — non-blocking hint. */
const conflicts = computed(() =>
  allEvents.value.filter(
    (ev) =>
      ev.kind !== 'maintenance' &&
      ev.kind !== 'backup' && // backups are not blocked
      hasMaintenance(ev.date),
  ),
)

// --------------------------------------------------------------------------- //
// Date helpers
// --------------------------------------------------------------------------- //

function dateKey(d: Date): string {
  return d.toISOString().slice(0, 10)
}

function sameDay(a: Date, b: Date): boolean {
  return dateKey(a) === dateKey(b)
}

function isToday(d: Date): boolean {
  return sameDay(d, today)
}

// --------------------------------------------------------------------------- //
// Month cells
// --------------------------------------------------------------------------- //

const monthStart = computed(
  () => new Date(cursor.value.getFullYear(), cursor.value.getMonth(), 1),
)

const monthCells = computed(() => {
  const first = monthStart.value
  const startDow = first.getDay() || 7 // ISO: Mon=1
  const startOffset = startDow - 1 // cells before the 1st

  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()
  const totalCells = Math.ceil((startOffset + daysInMonth) / 7) * 7

  const rangeStart = new Date(first.getTime() - startOffset * 86400_000)
  const rangeEnd = new Date(rangeStart.getTime() + totalCells * 86400_000)
  const evsByDate = groupByDate(allEvents.value, rangeStart, rangeEnd)

  const cells = []
  for (let i = 0; i < totalCells; i++) {
    const date = new Date(rangeStart.getTime() + i * 86400_000)
    cells.push({
      key: dateKey(date),
      date,
      dayNum: date.getDate(),
      currentMonth: date.getMonth() === first.getMonth(),
      isToday: isToday(date),
      events: evsByDate.get(dateKey(date)) ?? [],
    })
  }
  return cells
})

// --------------------------------------------------------------------------- //
// Week days
// --------------------------------------------------------------------------- //

const weekDays = computed(() => {
  const mon = mondayOfCursor.value
  const rangeEnd = new Date(mon.getTime() + 7 * 86400_000)
  const evsByDate = groupByDate(allEvents.value, mon, rangeEnd)

  return Array.from({ length: 7 }, (_, i) => {
    const date = new Date(mon.getTime() + i * 86400_000)
    return {
      key: dateKey(date),
      date,
      label: date.toLocaleDateString(undefined, { weekday: 'short' }),
      dayNum: date.getDate(),
      isToday: isToday(date),
      events: evsByDate.get(dateKey(date)) ?? [],
    }
  })
})

// --------------------------------------------------------------------------- //
// All events
// --------------------------------------------------------------------------- //

const allEvents = computed<CalendarEvent[]>(() => {
  const start =
    mode.value === 'month'
      ? new Date(
          cursor.value.getFullYear(),
          cursor.value.getMonth(),
          1 - ((new Date(cursor.value.getFullYear(), cursor.value.getMonth(), 1).getDay() || 7) - 1),
        )
      : mondayOfCursor.value
  const end =
    mode.value === 'month'
      ? new Date(cursor.value.getFullYear(), cursor.value.getMonth() + 1, 7)
      : new Date(mondayOfCursor.value.getTime() + 7 * 86400_000)

  const events: CalendarEvent[] = []

  // Schedule occurrences
  for (const s of props.schedules) {
    const occurrences = nextOccurrences(s, start, end)
    for (const occ of occurrences) {
      const kind = actionKind(s.action_name)
      const isConflict =
        kind !== 'backup' && _maintenanceDaysCache.value.has(dateKey(occ))
      events.push({
        title: s.name,
        typeLabel: actionTypeLabel(s.action_name),
        subtitle: s.target_label ?? undefined,
        date: occ,
        kind,
        conflict: isConflict,
      })
    }
  }

  // Maintenance window occurrences — one event per matching day per window
  for (const mw of props.maintenanceWindows) {
    if (!mw.enabled) continue
    const blockedLabel =
      mw.blocked_danger_classes.length > 0
        ? mw.blocked_danger_classes.join(', ')
        : 'nothing (no classes set)'
    const serverName = props.serverNames?.[mw.server_id] ?? `server #${mw.server_id}`
    const durationLabel =
      mw.duration_minutes >= 60
        ? `${Math.round(mw.duration_minutes / 60)}h`
        : `${mw.duration_minutes}m`
    const dowMatch = parseCronDow(mw.cron)
    const d = new Date(start)
    while (d <= end) {
      const isoDow = d.getDay() || 7
      const matches =
        (mw.is_active_now && sameDay(d, today)) ||
        (dowMatch && dowMatch.has(isoDow))
      if (matches) {
        events.push({
          title: mw.name,
          typeLabel: 'Maintenance window',
          subtitle: `${serverName} — blocks: ${blockedLabel}`,
          date: new Date(d),
          kind: 'maintenance',
          duration: durationLabel,
          conflict: false,
        })
      }
      d.setDate(d.getDate() + 1)
    }
  }

  return events
})

function groupByDate(
  events: CalendarEvent[],
  _start: Date,
  _end: Date,
): Map<string, CalendarEvent[]> {
  const map = new Map<string, CalendarEvent[]>()
  for (const ev of events) {
    const k = dateKey(ev.date)
    const bucket = map.get(k) ?? []
    bucket.push(ev)
    map.set(k, bucket)
  }
  return map
}

// --------------------------------------------------------------------------- //
// Helpers for template
// --------------------------------------------------------------------------- //

function eventColor(ev: CalendarEvent): string {
  switch (ev.kind) {
    case 'backup':
      return 'bg-ok/70'
    case 'restore_test':
      return 'bg-info/70'
    case 'update':
      return 'bg-warn/70'
    case 'maintenance':
      return 'bg-err/30'
  }
}

function formatEventTime(ev: CalendarEvent): string {
  return ev.date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

// --------------------------------------------------------------------------- //
// CalendarEvent sub-component (inline for locality)
// --------------------------------------------------------------------------- //

const CalendarEvent = defineComponent({
  props: {
    event: { type: Object as () => CalendarEvent, required: true },
  },
  emits: ['click'],
  setup(props, { emit }) {
    function bg() {
      switch (props.event.kind) {
        case 'backup':
          return 'bg-ok/15 border-ok/30 text-ok hover:bg-ok/25'
        case 'restore_test':
          return 'bg-info/15 border-info/30 text-info hover:bg-info/25'
        case 'update':
          return 'bg-warn/15 border-warn/30 text-warn hover:bg-warn/25'
        case 'maintenance':
          return 'bg-err/10 border-err/20 text-err hover:bg-err/20'
      }
    }
    return () =>
      h(
        'button',
        {
          type: 'button',
          class: `fdm-focus flex w-full items-center gap-1 truncate rounded border px-1 py-0.5 text-meta transition ${bg()}`,
          onClick: () => emit('click'),
        },
        [
          props.event.conflict
            ? h('span', { class: 'text-err flex-shrink-0' }, '⚠')
            : null,
          h('span', { class: 'truncate' }, props.event.title),
        ],
      )
  },
})
</script>
