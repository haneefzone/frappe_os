import { ref } from 'vue'

/**
 * Toast service (uiux-spec B5: toasts only for background events).
 * Import { toast } anywhere; <ToastHost /> in App.vue renders the stack.
 */

export type ToastType = 'info' | 'success' | 'error' | 'warning'

export interface ToastItem {
  id: number
  type: ToastType
  message: string
  title?: string
  /** Seconds; errors default to sticky-ish 8s, others 5s. */
  duration: number
}

export const toasts = ref<ToastItem[]>([])

let nextId = 1

function push(type: ToastType, message: string, opts?: { title?: string; duration?: number }) {
  const item: ToastItem = {
    id: nextId++,
    type,
    message,
    title: opts?.title,
    duration: opts?.duration ?? (type === 'error' ? 8 : 5),
  }
  toasts.value.push(item)
  window.setTimeout(() => dismiss(item.id), item.duration * 1000)
  return item.id
}

export function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

export const toast = {
  info: (message: string, opts?: { title?: string; duration?: number }) =>
    push('info', message, opts),
  success: (message: string, opts?: { title?: string; duration?: number }) =>
    push('success', message, opts),
  error: (message: string, opts?: { title?: string; duration?: number }) =>
    push('error', message, opts),
  warning: (message: string, opts?: { title?: string; duration?: number }) =>
    push('warning', message, opts),
}
