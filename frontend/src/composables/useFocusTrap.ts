import { type Ref } from 'vue'

const FOCUSABLE_SELECTOR =
  'a[href]:not([disabled]), button:not([disabled]), input:not([disabled]), ' +
  'textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function useFocusTrap(el: Ref<HTMLElement | undefined>) {
  function focusable() {
    return Array.from(el.value?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [])
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key !== 'Tab') return
    const els = focusable()
    if (!els.length) { e.preventDefault(); return }
    const first = els[0]
    const last = els[els.length - 1]
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus() }
    } else {
      if (document.activeElement === last) { e.preventDefault(); first.focus() }
    }
  }

  function activate() {
    el.value?.addEventListener('keydown', onKeyDown)
    focusable()[0]?.focus()
  }

  function deactivate() {
    el.value?.removeEventListener('keydown', onKeyDown)
  }

  return { activate, deactivate }
}
