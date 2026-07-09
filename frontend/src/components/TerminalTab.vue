<template>
  <div
    v-show="active"
    class="absolute inset-0 flex flex-col bg-base"
    :aria-hidden="!active"
  >
    <!-- Context bar -->
    <div class="flex shrink-0 items-center justify-between gap-3 border-b border-line px-4 py-2">
      <div class="flex items-center gap-2 text-label text-ink-2 font-mono">
        <LucideTerminal class="h-3.5 w-3.5 text-ink-3" />
        <span>{{ tab.sshUsername }}@{{ tab.serverName }}</span>
        <span v-if="tab.idleWarning" class="text-warn text-meta">
          ⏱ idle warning
        </span>
        <span v-if="tab.status === 'disconnected'" class="text-error text-meta">
          disconnected
        </span>
      </div>

      <div class="flex items-center gap-1">
        <button
          v-for="cmd in quickInserts"
          :key="cmd.label"
          type="button"
          class="
            fdm-focus rounded border border-line bg-raised px-2 py-0.5 font-mono text-meta
            text-ink-2 transition hover:border-border-strong hover:text-ink-1
          "
          :title="`Insert: ${cmd.text}`"
          @click="insertText(cmd.text)"
        >
          {{ cmd.label }}
        </button>
      </div>
    </div>

    <!-- xterm.js mount point -->
    <div
      ref="terminalEl"
      class="min-h-0 flex-1 overflow-hidden"
      :style="{ padding: '4px' }"
    />

    <!-- Reconnect overlay (disconnected state) -->
    <div
      v-if="tab.status === 'disconnected'"
      class="
        absolute inset-0 flex flex-col items-center justify-center gap-3
        bg-base/80 backdrop-blur-sm
      "
    >
      <LucideWifi class="h-8 w-8 text-error" />
      <p class="text-body font-medium text-ink-1">Connection closed</p>
      <Button label="Reconnect" variant="solid" theme="gray" @click="$emit('reconnect')" />
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  ref,
  watch,
  onMounted,
  onBeforeUnmount,
  nextTick,
} from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import LucideTerminal from '~icons/lucide/terminal'
import LucideWifi from '~icons/lucide/wifi'
import { Button } from 'frappe-ui'
import '@xterm/xterm/css/xterm.css'
import type { TerminalTabState } from '../pages/TerminalPage.vue'

const props = defineProps<{
  tab: TerminalTabState
  active: boolean
}>()

const emit = defineEmits<{
  (e: 'idle-warning'): void
  (e: 'disconnected'): void
  (e: 'reconnect'): void
}>()

const terminalEl = ref<HTMLElement | null>(null)

const quickInserts = [
  { label: 'cd bench', text: 'cd ~/frappe-bench' },
  { label: 'bench --site', text: 'bench --site ' },
  { label: 'sudo -i', text: 'sudo -i\n' },
]

let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let ws: WebSocket | null = null
let resizeObserver: ResizeObserver | null = null

function insertText(text: string) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(new TextEncoder().encode(text))
  }
}

function connect(wsUrl: string) {
  if (ws) {
    ws.onclose = null
    ws.onerror = null
    ws.close()
    ws = null
  }

  ws = new WebSocket(wsUrl)
  ws.binaryType = 'arraybuffer'

  ws.onopen = () => {
    props.tab.status = 'open'
    fitAndSendResize()
  }

  ws.onmessage = (ev) => {
    if (!term) return
    if (ev.data instanceof ArrayBuffer) {
      term.write(new Uint8Array(ev.data))
    } else if (typeof ev.data === 'string') {
      // Check for inline idle warning injected by the backend.
      if (ev.data.includes('[FDM] Idle for')) {
        emit('idle-warning')
      }
      term.write(ev.data)
    }
  }

  ws.onclose = () => {
    props.tab.status = 'disconnected'
    emit('disconnected')
  }

  ws.onerror = () => {
    props.tab.status = 'disconnected'
    emit('disconnected')
  }
}

function fitAndSendResize() {
  if (!fitAddon || !term || !ws || ws.readyState !== WebSocket.OPEN) return
  fitAddon.fit()
  ws.send(
    JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }),
  )
}

function initTerminal() {
  if (!terminalEl.value) return

  term = new Terminal({
    theme: {
      background: '#0A0A0B',
      foreground: '#F4F4F5',
      cursor: '#F4F4F5',
      selectionBackground: '#2E2E33',
      black: '#111113',
      brightBlack: '#6B6B74',
    },
    fontFamily: '"JetBrains Mono", monospace',
    fontSize: 13,
    lineHeight: 1.4,
    cursorBlink: true,
    allowProposedApi: true,
  })

  fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  term.open(terminalEl.value)
  fitAddon.fit()

  // Forward keyboard input to the WebSocket.
  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data))
    }
  })

  // Forward binary input (e.g. paste) too.
  term.onBinary((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      const bytes = new Uint8Array(data.length)
      for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i) & 0xff
      ws.send(bytes)
    }
  })

  // Resize observer: refit when the container changes dimensions.
  resizeObserver = new ResizeObserver(() => fitAndSendResize())
  resizeObserver.observe(terminalEl.value)

  connect(props.tab.wsUrl)
}

onMounted(async () => {
  await nextTick()
  initTerminal()
})

// When the parent issues a new wsUrl (reconnect flow), re-connect.
watch(
  () => props.tab.wsUrl,
  (newUrl) => {
    if (term) {
      term.clear()
    } else {
      initTerminal()
    }
    connect(newUrl)
  },
)

// When this tab becomes active, fit the terminal to the (possibly
// newly-visible) container.
watch(
  () => props.active,
  async (isActive) => {
    if (isActive) {
      await nextTick()
      fitAndSendResize()
      term?.focus()
    }
  },
)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  ws?.close()
  term?.dispose()
})
</script>
