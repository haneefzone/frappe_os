import { defineStore } from 'pinia'
import { type NotificationItem, notificationsApi } from '../api/notifications'

const POLL_INTERVAL_MS = 30_000

export const useNotificationsStore = defineStore('notifications', {
  state: () => ({
    items: [] as NotificationItem[],
    unreadCount: 0,
    loading: false,
    lastFetched: 0,
    _pollTimer: null as ReturnType<typeof setInterval> | null,
  }),

  actions: {
    async fetch() {
      this.loading = true
      try {
        const data = await notificationsApi.list({ limit: 50 })
        this.items = data.items
        this.unreadCount = data.unread_count
        this.lastFetched = Date.now()
      } catch {
        // silent — bell stays at last count
      } finally {
        this.loading = false
      }
    },

    startPolling() {
      if (this._pollTimer !== null) return
      void this.fetch()
      this._pollTimer = setInterval(() => void this.fetch(), POLL_INTERVAL_MS)
    },

    stopPolling() {
      if (this._pollTimer !== null) {
        clearInterval(this._pollTimer)
        this._pollTimer = null
      }
    },

    async markRead(id: number) {
      await notificationsApi.markRead(id)
      const n = this.items.find((i) => i.id === id)
      if (n && !n.read) {
        n.read = true
        this.unreadCount = Math.max(0, this.unreadCount - 1)
      }
    },

    async markAllRead() {
      await notificationsApi.markAllRead()
      this.items.forEach((n) => (n.read = true))
      this.unreadCount = 0
    },

    async dismiss(id: number) {
      await notificationsApi.dismiss(id)
      const idx = this.items.findIndex((i) => i.id === id)
      if (idx !== -1) {
        if (!this.items[idx]!.read) this.unreadCount = Math.max(0, this.unreadCount - 1)
        this.items.splice(idx, 1)
      }
    },
  },
})
