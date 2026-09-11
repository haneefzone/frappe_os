/**
 * Typed data layer for the AlertRule engine (session 3.1, CLAUDE.md rule 9).
 * The webhook signing secret is write-only: accepted on create/update,
 * encrypted server-side, and never returned — the read model exposes only
 * `webhook_secret_set` (rule 6). Every mutating call goes through the typed
 * API client (session auth cookie + CSRF).
 */

import { apiClient } from './client'

export type AlertMetric = 'cpu_pct' | 'mem_pct' | 'disk_pct' | 'load1'
export type AlertComparator = '>' | '>=' | '<' | '<=' | '=='
export type AlertScope = 'global' | 'server'

export interface AlertRule {
  id: number
  name: string
  metric: AlertMetric
  comparator: AlertComparator
  threshold: number
  scope: AlertScope
  scope_server_id: number | null
  cooldown_minutes: number
  channel_email: boolean
  channel_webhook: boolean
  email_to: string | null
  webhook_url: string | null
  webhook_secret_set: boolean
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ChannelOutcome {
  channel: string
  ok: boolean
  error: string | null
}

export interface AlertFiring {
  id: number
  rule_id: number | null
  rule_name: string | null
  server_id: number | null
  server_name: string | null
  metric: string
  comparator: string
  threshold: number
  value: number | null
  channels: ChannelOutcome[]
  delivered_at: string | null
  resolved_at: string | null
  created_at: string
}

export interface IncidentsData {
  open_alerts: number
  incidents: AlertFiring[]
}

export interface AlertRuleCreatePayload {
  name: string
  metric: AlertMetric
  comparator: AlertComparator
  threshold: number
  scope: AlertScope
  scope_server_id?: number | null
  cooldown_minutes: number
  channel_email: boolean
  channel_webhook: boolean
  email_to?: string | null
  webhook_url?: string | null
  webhook_secret?: string | null
  enabled: boolean
}

export interface AlertRuleUpdatePayload {
  name?: string
  metric?: AlertMetric
  comparator?: AlertComparator
  threshold?: number
  scope?: AlertScope
  scope_server_id?: number | null
  cooldown_minutes?: number
  channel_email?: boolean
  channel_webhook?: boolean
  email_to?: string | null
  webhook_url?: string | null
  webhook_secret?: string | null
  enabled?: boolean
}

export const METRIC_LABELS: Record<AlertMetric, string> = {
  cpu_pct: 'CPU %',
  mem_pct: 'Memory %',
  disk_pct: 'Disk %',
  load1: 'Load (1m)',
}

export const METRIC_OPTIONS: { value: AlertMetric; label: string }[] = [
  { value: 'cpu_pct', label: 'CPU %' },
  { value: 'mem_pct', label: 'Memory %' },
  { value: 'disk_pct', label: 'Disk %' },
  { value: 'load1', label: 'Load (1m)' },
]

export const COMPARATOR_OPTIONS: { value: AlertComparator; label: string }[] = [
  { value: '>', label: '> (greater than)' },
  { value: '>=', label: '≥ (at least)' },
  { value: '<', label: '< (less than)' },
  { value: '<=', label: '≤ (at most)' },
  { value: '==', label: '= (equal)' },
]

export const alertsApi = {
  list: () => apiClient.get<AlertRule[]>('/api/alerts'),
  get: (id: number) => apiClient.get<AlertRule>(`/api/alerts/${id}`),
  create: (payload: AlertRuleCreatePayload) => apiClient.post<AlertRule>('/api/alerts', payload),
  update: (id: number, payload: AlertRuleUpdatePayload) =>
    apiClient.patch<AlertRule>(`/api/alerts/${id}`, payload),
  remove: (id: number) => apiClient.delete<void>(`/api/alerts/${id}`),
  test: (id: number) => apiClient.post<AlertFiring>(`/api/alerts/${id}/test`),
  incidents: (limit = 20) => apiClient.get<IncidentsData>(`/api/alerts/incidents?limit=${limit}`),
}
