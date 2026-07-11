<template>
  <div class="overflow-hidden rounded-lg border border-line bg-base">
    <div
      v-if="!diff || !diff.trim()"
      class="px-4 py-10 text-center text-label text-ink-3"
    >
      No changes were made in this session.
    </div>
    <div v-else class="max-h-[60vh] overflow-auto">
      <pre class="m-0 font-mono text-meta leading-relaxed"><code><span
        v-for="(line, i) in lines"
        :key="i"
        class="block whitespace-pre px-4"
        :class="lineClass(line.kind)"
      >{{ line.text || ' ' }}</span></code></pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type LineKind = 'add' | 'del' | 'hunk' | 'meta' | 'context'

const props = defineProps<{ diff: string | null }>()

interface DiffLine {
  text: string
  kind: LineKind
}

function classify(text: string): LineKind {
  if (text.startsWith('@@')) return 'hunk'
  if (
    text.startsWith('diff ') ||
    text.startsWith('index ') ||
    text.startsWith('--- ') ||
    text.startsWith('+++ ') ||
    text.startsWith('new file') ||
    text.startsWith('deleted file') ||
    text.startsWith('similarity ') ||
    text.startsWith('rename ')
  ) {
    return 'meta'
  }
  if (text.startsWith('+')) return 'add'
  if (text.startsWith('-')) return 'del'
  return 'context'
}

const lines = computed<DiffLine[]>(() =>
  (props.diff ?? '')
    .replace(/\n$/, '')
    .split('\n')
    .map((text) => ({ text, kind: classify(text) })),
)

// Status colors only (design system): added = ok green, removed = err red,
// hunk/meta headers muted.
function lineClass(kind: LineKind): string {
  switch (kind) {
    case 'add':
      return 'bg-ok/10 text-ok'
    case 'del':
      return 'bg-err/10 text-err'
    case 'hunk':
      return 'text-run'
    case 'meta':
      return 'text-ink-3'
    default:
      return 'text-ink-2'
  }
}
</script>
