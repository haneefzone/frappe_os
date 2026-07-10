/**
 * Global keyboard shortcuts (CLAUDE.md B5):
 *   ⌘K / Ctrl+K — command palette
 *   g d          — navigate to dashboard
 *   g j          — navigate to jobs
 *   t            — navigate to terminal
 *   /            — focus command palette search
 *   ?            — shortcut help sheet (not yet built; keyboard event captured)
 *
 * Call once from App.vue so the listener is globally active on authenticated screens.
 */

import { onBeforeUnmount, onMounted, type Ref } from 'vue'
import { useRouter } from 'vue-router'

export interface KeyboardShortcutsOptions {
  /** Ref to the CommandPalette component exposing `openPalette()`. */
  palette: Ref<{ openPalette: () => void } | null>
}

export function useKeyboardShortcuts({ palette }: KeyboardShortcutsOptions) {
  const router = useRouter()
  let _gPressed = false
  let _gTimer: ReturnType<typeof setTimeout> | null = null

  function onKeydown(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement)?.tagName?.toLowerCase()
    const isInput = tag === 'input' || tag === 'textarea' || (e.target as HTMLElement)?.isContentEditable

    // ⌘K / Ctrl+K — open palette regardless of input focus.
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault()
      palette.value?.openPalette()
      return
    }

    // Skip g-chord and other shortcuts when user is typing in an input.
    if (isInput) return

    // g-chord: g then d = dashboard, g then j = jobs.
    if (e.key === 'g' && !e.metaKey && !e.ctrlKey) {
      _gPressed = true
      if (_gTimer) clearTimeout(_gTimer)
      _gTimer = setTimeout(() => { _gPressed = false }, 800)
      return
    }

    if (_gPressed) {
      _gPressed = false
      if (_gTimer) clearTimeout(_gTimer)
      if (e.key === 'd') { e.preventDefault(); router.push('/') }
      else if (e.key === 'j') { e.preventDefault(); router.push('/jobs') }
      return
    }

    // Single-key shortcuts.
    if (e.key === 't' && !e.metaKey && !e.ctrlKey) {
      e.preventDefault()
      router.push('/terminal')
    } else if (e.key === '/' && !e.metaKey && !e.ctrlKey) {
      e.preventDefault()
      palette.value?.openPalette()
    }
  }

  onMounted(() => document.addEventListener('keydown', onKeydown))
  onBeforeUnmount(() => {
    document.removeEventListener('keydown', onKeydown)
    if (_gTimer) clearTimeout(_gTimer)
  })
}
