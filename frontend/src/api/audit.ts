/**
 * Typed data layer for the audit log (session 1.12, CLAUDE.md rules 2 + 9):
 * an immutable, filterable record of every state-changing action, exportable
 * as CSV. The CSV endpoint is a plain authenticated GET the caller downloads.
 */

import { apiClient } from './client'

export type AuditResult = 'ok' | 'enqueued' | 'denied' | 'error' | string

export interface AuditEntry {
  id: number
  ts: string
  user_id: number | null
  user_email: string
  action: string
  entity_type: string
  entity_id: string | null
  summary: string
  params_masked: Record<string, unknown>
  result: AuditResult
  source_ip: string | null
  job_id: number | null
}

export interface AuditResponse {
  entries: AuditEntry[]
  total: number
  limit: number
  offset: number
}

export interface AuditFilters {
  action?: string
  entity_type?: string
  result?: string
  user_id?: number
  since?: string
  until?: string
  q?: string
  limit?: number
  offset?: number
}

export function auditQuery(filters: AuditFilters = {}): string {
  const q = new URLSearchParams()
  if (filters.action) q.set('action', filters.action)
  if (filters.entity_type) q.set('entity_type', filters.entity_type)
  if (filters.result) q.set('result', filters.result)
  if (filters.user_id != null) q.set('user_id', String(filters.user_id))
  if (filters.since) q.set('since', filters.since)
  if (filters.until) q.set('until', filters.until)
  if (filters.q) q.set('q', filters.q)
  if (filters.limit != null) q.set('limit', String(filters.limit))
  if (filters.offset != null) q.set('offset', String(filters.offset))
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const auditApi = {
  list: (filters?: AuditFilters) =>
    apiClient.get<AuditResponse>(`/api/audit${auditQuery(filters)}`),
  /** CSV export URL for the current filters (fetched + downloaded as a blob). */
  csvUrl: (filters?: AuditFilters) => `/api/audit.csv${auditQuery(filters)}`,
}
