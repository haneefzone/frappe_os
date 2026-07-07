<template>
  <div class="overflow-hidden rounded-lg border border-line bg-surface">
    <!-- Toolbar: text filter · column chooser · density toggle -->
    <div class="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
      <div class="relative min-w-0 flex-1" style="max-width: 280px">
        <LucideSearch class="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
        <input
          v-model="filter"
          type="text"
          :placeholder="filterPlaceholder"
          class="fdm-focus w-full rounded-lg border border-line bg-base py-1 pl-7 pr-2 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
        />
      </div>

      <div class="ml-auto flex items-center gap-1">
        <!-- Column chooser -->
        <div ref="chooserRoot" class="relative">
          <button
            type="button"
            class="fdm-focus flex items-center gap-1.5 rounded-lg border border-line px-2 py-1 text-meta font-medium text-ink-2 transition hover:border-line-strong hover:text-ink-1"
            :aria-expanded="chooserOpen"
            @click="chooserOpen = !chooserOpen"
          >
            <LucideColumns3 class="h-3.5 w-3.5" />
            Columns
          </button>
          <div
            v-if="chooserOpen"
            class="absolute right-0 top-full z-20 mt-1 w-48 rounded-lg border border-line bg-raised py-1"
          >
            <label
              v-for="col in columns"
              :key="col.key"
              class="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-label text-ink-1 transition hover:bg-surface"
            >
              <input
                type="checkbox"
                class="h-3.5 w-3.5 rounded border-line"
                :checked="visibleKeys.has(col.key)"
                :disabled="visibleKeys.has(col.key) && visibleKeys.size === 1"
                @change="toggleColumn(col.key)"
              />
              {{ col.label }}
            </label>
          </div>
        </div>

        <!-- Density toggle -->
        <button
          type="button"
          class="fdm-focus flex items-center gap-1.5 rounded-lg border border-line px-2 py-1 text-meta font-medium text-ink-2 transition hover:border-line-strong hover:text-ink-1"
          :aria-label="`Switch to ${density === 'compact' ? 'comfortable' : 'compact'} density`"
          @click="density = density === 'compact' ? 'comfortable' : 'compact'"
        >
          <LucideRows3 v-if="density === 'compact'" class="h-3.5 w-3.5" />
          <LucideRows4 v-else class="h-3.5 w-3.5" />
          {{ density === 'compact' ? 'Compact' : 'Comfortable' }}
        </button>
      </div>
    </div>

    <!-- Table -->
    <div class="overflow-auto" :style="{ maxHeight: height }">
      <table class="w-full border-collapse text-left">
        <thead class="sticky top-0 z-10">
          <tr class="bg-surface shadow-[inset_0_-1px_0_var(--border)]">
            <th
              v-for="col in visibleColumns"
              :key="col.key"
              class="whitespace-nowrap px-3 text-meta font-medium uppercase tracking-wide text-ink-2"
              :class="[headerPad, alignClass(col)]"
              :style="col.width ? { width: col.width } : {}"
            >
              <button
                v-if="col.sortable"
                type="button"
                class="fdm-focus inline-flex items-center gap-1 rounded uppercase tracking-wide transition hover:text-ink-1"
                @click="cycleSort(col.key)"
              >
                {{ col.label }}
                <LucideArrowUp v-if="sortKey === col.key && sortDir === 'asc'" class="h-3 w-3" />
                <LucideArrowDown v-else-if="sortKey === col.key && sortDir === 'desc'" class="h-3 w-3" />
                <LucideArrowUpDown v-else class="h-3 w-3 opacity-40" />
              </button>
              <template v-else>{{ col.label }}</template>
            </th>
            <th v-if="$slots.actions" class="w-px whitespace-nowrap px-3" :class="headerPad">
              <span class="sr-only">Row actions</span>
            </th>
          </tr>
        </thead>

        <!-- Loading: skeleton rows -->
        <tbody v-if="loading">
          <tr v-for="i in 8" :key="i" class="border-t border-line">
            <td v-for="col in visibleColumns" :key="col.key" class="px-3" :class="cellPad">
              <div class="h-3.5 animate-pulse rounded bg-raised" :style="{ width: `${45 + ((i * 17) % 40)}%` }" />
            </td>
            <td v-if="$slots.actions" class="px-3" :class="cellPad">
              <div class="h-3.5 w-6 animate-pulse rounded bg-raised" />
            </td>
          </tr>
        </tbody>

        <tbody v-else-if="processedRows.length">
          <tr
            v-for="row in processedRows"
            :key="keyOf(row)"
            class="border-t border-line transition-colors hover:bg-raised"
            style="content-visibility: auto"
            :style="{ containIntrinsicSize: `auto ${density === 'compact' ? 33 : 41}px` }"
          >
            <td
              v-for="col in visibleColumns"
              :key="col.key"
              class="px-3 text-ink-1"
              :class="[cellPad, alignClass(col), density === 'compact' ? 'text-label' : 'text-body']"
            >
              <slot :name="`cell-${col.key}`" :row="row" :value="displayValue(row, col)">
                {{ displayValue(row, col) }}
              </slot>
            </td>
            <td v-if="$slots.actions" class="whitespace-nowrap px-3 text-right" :class="cellPad">
              <slot name="actions" :row="row" />
            </td>
          </tr>
        </tbody>

        <!-- Empty state -->
        <tbody v-else>
          <tr>
            <td :colspan="visibleColumns.length + ($slots.actions ? 1 : 0)">
              <slot name="empty">
                <EmptyState
                  :icon="LucideInbox"
                  :title="filter ? 'No matching rows' : emptyTitle"
                  :message="filter ? `Nothing matches “${filter}”.` : emptyMessage"
                />
              </slot>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Footer -->
    <div class="flex items-center justify-between border-t border-line px-3 py-1.5 text-meta tabular-nums text-ink-2">
      <span>{{ processedRows.length.toLocaleString() }} of {{ rows.length.toLocaleString() }} rows</span>
      <span v-if="sortKey">sorted by {{ columnLabel(sortKey) }} {{ sortDir === 'asc' ? '↑' : '↓' }}</span>
    </div>
  </div>
</template>

<script setup lang="ts" generic="Row extends Record<string, unknown>">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import LucideArrowDown from '~icons/lucide/arrow-down'
import LucideArrowUp from '~icons/lucide/arrow-up'
import LucideArrowUpDown from '~icons/lucide/arrow-up-down'
import LucideColumns3 from '~icons/lucide/columns-3'
import LucideInbox from '~icons/lucide/inbox'
import LucideRows3 from '~icons/lucide/rows-3'
import LucideRows4 from '~icons/lucide/rows-4'
import LucideSearch from '~icons/lucide/search'
import EmptyState from './EmptyState.vue'
import type { DataTableColumn } from './types'

const props = withDefaults(
  defineProps<{
    columns: DataTableColumn<Row>[]
    rows: Row[]
    /** Row identity for keying; a row property name or a function. */
    rowKey: keyof Row | ((row: Row) => string | number)
    loading?: boolean
    /** Max body height; the header stays stuck while it scrolls. */
    height?: string
    defaultDensity?: 'compact' | 'comfortable'
    filterPlaceholder?: string
    emptyTitle?: string
    emptyMessage?: string
  }>(),
  {
    loading: false,
    height: '480px',
    defaultDensity: 'comfortable',
    filterPlaceholder: 'Filter rows',
    emptyTitle: 'Nothing here yet',
    emptyMessage: 'Rows will appear here once there is data.',
  },
)

const filter = ref('')
const density = ref<'compact' | 'comfortable'>(props.defaultDensity)
const sortKey = ref<string | null>(null)
const sortDir = ref<'asc' | 'desc'>('asc')

// Column visibility
const visibleKeys = ref(new Set(props.columns.filter((c) => !c.hidden).map((c) => c.key)))
const chooserOpen = ref(false)
const chooserRoot = ref<HTMLElement | null>(null)

function toggleColumn(key: string) {
  const next = new Set(visibleKeys.value)
  if (next.has(key)) {
    if (next.size === 1) return // never hide the last column
    next.delete(key)
  } else {
    next.add(key)
  }
  visibleKeys.value = next
}

function onDocClick(e: MouseEvent) {
  if (chooserOpen.value && chooserRoot.value && !chooserRoot.value.contains(e.target as Node)) {
    chooserOpen.value = false
  }
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

const visibleColumns = computed(() => props.columns.filter((c) => visibleKeys.value.has(c.key)))

function columnLabel(key: string) {
  return props.columns.find((c) => c.key === key)?.label ?? key
}

function keyOf(row: Row): string | number {
  return typeof props.rowKey === 'function'
    ? props.rowKey(row)
    : (row[props.rowKey] as string | number)
}

function displayValue(row: Row, col: DataTableColumn<Row>): string {
  if (col.format) return col.format(row)
  const v = row[col.key]
  return v === null || v === undefined ? '' : String(v)
}

function alignClass(col: DataTableColumn<Row>) {
  return col.align === 'right' ? 'text-right' : col.align === 'center' ? 'text-center' : 'text-left'
}

function cycleSort(key: string) {
  if (sortKey.value !== key) {
    sortKey.value = key
    sortDir.value = 'asc'
  } else if (sortDir.value === 'asc') {
    sortDir.value = 'desc'
  } else {
    sortKey.value = null
  }
}

// Filter across visible columns (on displayed values), then sort.
const processedRows = computed(() => {
  let out = props.rows
  const q = filter.value.trim().toLowerCase()
  if (q) {
    const cols = visibleColumns.value
    out = out.filter((row) => cols.some((c) => displayValue(row, c).toLowerCase().includes(q)))
  }
  const key = sortKey.value
  if (key) {
    const col = props.columns.find((c) => c.key === key)
    const dir = sortDir.value === 'asc' ? 1 : -1
    out = [...out].sort((a, b) => {
      const ra = col?.format ? col.format(a) : a[key]
      const rb = col?.format ? col.format(b) : b[key]
      if (typeof ra === 'number' && typeof rb === 'number') return (ra - rb) * dir
      return String(ra ?? '').localeCompare(String(rb ?? ''), undefined, { numeric: true }) * dir
    })
  }
  return out
})

const headerPad = computed(() => (density.value === 'compact' ? 'py-1.5' : 'py-2.5'))
const cellPad = computed(() => (density.value === 'compact' ? 'py-1.5' : 'py-2.5'))
</script>
