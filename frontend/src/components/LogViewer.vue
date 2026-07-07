<template>
  <div
    class="flex flex-col overflow-hidden rounded-lg border border-line bg-surface"
    :class="fullscreen ? 'fixed inset-4 z-50' : ''"
    :style="fullscreen ? {} : { height }"
    @keydown.esc="fullscreen = false"
  >
    <!-- Toolbar -->
    <div class="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
      <span v-if="title" class="mr-1 text-label font-medium text-ink-1">{{ title }}</span>

      <div class="relative min-w-0 flex-1" style="max-width: 260px">
        <LucideSearch class="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" />
        <input
          v-model="query"
          type="text"
          placeholder="Search logs"
          class="fdm-focus w-full rounded-lg border border-line bg-base py-1 pl-7 pr-2 text-label text-ink-1 placeholder:text-ink-3 focus:border-line-strong"
        />
      </div>
      <span v-if="query" class="text-meta tabular-nums text-ink-3">
        {{ matchCount }} {{ matchCount === 1 ? 'match' : 'matches' }}
      </span>

      <div class="ml-auto flex items-center gap-1">
        <button
          type="button"
          class="fdm-focus flex items-center gap-1.5 rounded-lg border px-2 py-1 text-meta font-medium transition"
          :class="
            follow
              ? 'border-run/40 bg-run/10 text-run'
              : 'border-line text-ink-2 hover:border-line-strong hover:text-ink-1'
          "
          :aria-pressed="follow"
          @click="toggleFollow"
        >
          <LucideArrowDownToLine class="h-3.5 w-3.5" />
          Follow tail
        </button>
        <button
          type="button"
          class="fdm-focus rounded-lg border border-line p-1.5 text-ink-2 transition hover:border-line-strong hover:text-ink-1"
          aria-label="Download log"
          @click="download"
        >
          <LucideDownload class="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          class="fdm-focus rounded-lg border border-line p-1.5 text-ink-2 transition hover:border-line-strong hover:text-ink-1"
          :aria-label="fullscreen ? 'Exit fullscreen' : 'Fullscreen'"
          @click="fullscreen = !fullscreen"
        >
          <LucideMinimize2 v-if="fullscreen" class="h-3.5 w-3.5" />
          <LucideMaximize2 v-else class="h-3.5 w-3.5" />
        </button>
      </div>
    </div>

    <!-- Log body -->
    <div ref="scroller" class="min-h-0 flex-1 overflow-auto bg-base" @scroll="onScroll">
      <div v-if="lines.length === 0" class="px-4 py-8 text-center text-label text-ink-3">
        No log output yet.
      </div>
      <table v-else class="w-full border-collapse font-mono text-meta leading-[18px]">
        <tbody>
          <tr
            v-for="entry in visibleLines"
            :key="entry.n"
            class="align-top"
            :class="entry.isError ? 'bg-err/10' : ''"
            style="content-visibility: auto; contain-intrinsic-size: auto 18px"
          >
            <td
              class="w-12 select-none border-r border-line pr-2 text-right tabular-nums text-ink-3"
              :class="entry.isError ? 'text-err' : ''"
            >
              {{ entry.n }}
            </td>
            <td
              class="whitespace-pre-wrap break-all pl-3 pr-4"
              :class="entry.isError ? 'text-err' : 'text-ink-2'"
            >
              <template v-if="query && entry.parts">
                <template v-for="(part, pi) in entry.parts" :key="pi">
                  <mark
                    v-if="part.hit"
                    class="rounded-sm bg-warn/30 px-px text-ink-1"
                    >{{ part.text }}</mark
                  >
                  <template v-else>{{ part.text }}</template>
                </template>
              </template>
              <template v-else>{{ entry.text }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Footer -->
    <div class="flex items-center justify-between border-t border-line px-3 py-1.5 text-meta text-ink-3">
      <span class="tabular-nums">{{ lines.length.toLocaleString() }} lines</span>
      <span v-if="query" class="tabular-nums">showing {{ visibleLines.length.toLocaleString() }} matching</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import LucideArrowDownToLine from '~icons/lucide/arrow-down-to-line'
import LucideDownload from '~icons/lucide/download'
import LucideMaximize2 from '~icons/lucide/maximize-2'
import LucideMinimize2 from '~icons/lucide/minimize-2'
import LucideSearch from '~icons/lucide/search'

const props = withDefaults(
  defineProps<{
    lines: string[]
    title?: string
    /** File name used by the download button. */
    filename?: string
    /** Panel height when not fullscreen. */
    height?: string
    /** Start with follow-tail on. */
    initialFollow?: boolean
  }>(),
  { filename: 'log.txt', height: '360px', initialFollow: false },
)

const query = ref('')
const follow = ref(props.initialFollow)
const fullscreen = ref(false)
const scroller = ref<HTMLElement | null>(null)

const ERROR_RE = /\b(error|failed|failure|fatal|critical|traceback|exception|panic)\b/i

interface Part {
  text: string
  hit: boolean
}

interface Entry {
  n: number
  text: string
  isError: boolean
  parts?: Part[]
}

function splitByQuery(text: string, q: string): Part[] {
  const parts: Part[] = []
  const lower = text.toLowerCase()
  const needle = q.toLowerCase()
  let pos = 0
  while (true) {
    const idx = lower.indexOf(needle, pos)
    if (idx === -1) {
      if (pos < text.length) parts.push({ text: text.slice(pos), hit: false })
      break
    }
    if (idx > pos) parts.push({ text: text.slice(pos, idx), hit: false })
    parts.push({ text: text.slice(idx, idx + q.length), hit: true })
    pos = idx + q.length
  }
  return parts
}

// Searching filters to matching lines (keeping real line numbers) and
// highlights the matched text inside each line.
const visibleLines = computed<Entry[]>(() => {
  const q = query.value.trim()
  const out: Entry[] = []
  for (let i = 0; i < props.lines.length; i++) {
    const text = props.lines[i]
    if (q && !text.toLowerCase().includes(q.toLowerCase())) continue
    out.push({
      n: i + 1,
      text,
      isError: ERROR_RE.test(text),
      parts: q ? splitByQuery(text, q) : undefined,
    })
  }
  return out
})

const matchCount = computed(() => (query.value.trim() ? visibleLines.value.length : 0))

function scrollToBottom() {
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

function toggleFollow() {
  follow.value = !follow.value
  if (follow.value) scrollToBottom()
}

// A manual scroll away from the bottom releases follow-tail.
function onScroll() {
  const el = scroller.value
  if (!el || !follow.value) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  if (!atBottom) follow.value = false
}

watch(
  () => props.lines.length,
  () => {
    if (follow.value) nextTick(scrollToBottom)
  },
)

onMounted(() => {
  if (follow.value) scrollToBottom()
})

function download() {
  const blob = new Blob([props.lines.join('\n')], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = props.filename
  a.click()
  URL.revokeObjectURL(url)
}
</script>
