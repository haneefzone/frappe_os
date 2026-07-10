import { apiClient } from './client'

export interface NotificationItem {
  id: number
  event_type: string
  title: string
  body: string
  entity_type: string | null
  entity_id: number | null
  read: boolean
  created_at: string
}

export interface NotificationList {
  items: NotificationItem[]
  unread_count: number
}

export interface NotificationPref {
  event_type: string
  channel_in_app: boolean
  channel_email: boolean
  channel_webhook: boolean
  webhook_url: string | null
}

export const notificationsApi = {
  list: (params?: { limit?: number; unread_only?: boolean }) => {
    const qs = new URLSearchParams()
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.unread_only) qs.set('unread_only', 'true')
    return apiClient.get<NotificationList>(`/api/notifications?${qs}`)
  },

  markRead: (id: number) =>
    apiClient.post<NotificationItem>(`/api/notifications/${id}/read`),

  markAllRead: () => apiClient.post<{ ok: boolean }>('/api/notifications/read-all'),

  dismiss: (id: number) =>
    apiClient.delete<{ ok: boolean }>(`/api/notifications/${id}`),

  getPreferences: () =>
    apiClient.get<NotificationPref[]>('/api/notifications/preferences'),

  setPreference: (
    eventType: string,
    data: Omit<NotificationPref, 'event_type'>,
  ) =>
    apiClient.put<NotificationPref>(
      `/api/notifications/preferences/${eventType}`,
      data,
    ),
}

export interface SearchResult {
  kind: 'server' | 'bench' | 'site' | 'job' | 'schedule' | 'nav' | 'action'
  id: string
  title: string
  subtitle?: string
  url?: string
  action?: {
    action_name: string
    target_type: string
    target_id: number
    params: Record<string, unknown>
  }
}

export const searchApi = {
  search: (q: string, limit = 20) =>
    apiClient.get<{ results: SearchResult[] }>(
      `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
}
