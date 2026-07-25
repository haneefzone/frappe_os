/**
 * Typed data layer for config-drift detection (session 6.7, rule 9).
 * All reads need only `read`; accept + runCheck need `server:manage`.
 */

import { apiClient } from './client'

export type DriftStatus = 'baseline' | 'drifted' | 'accepted'

export interface DriftBaseline {
  id: number
  server_id: number
  bench_id: number | null
  site_id: number | null
  artifact_key: string
  path: string
  status: DriftStatus
  sha256: string | null
  size: number
  current_sha256: string | null
  captured_at: string
  captured_by_job_id: number | null
  last_checked_at: string | null
  drift_detected_at: string | null
}

export interface DriftDiff {
  baseline: DriftBaseline
  hash_only: boolean
  unified_diff: string
}

export interface DriftSummary {
  drifted_count: number
  server_ids: number[]
}

export interface DriftListParams {
  server_id?: number
  status?: DriftStatus
  drifted_only?: boolean
}

export const driftApi = {
  list: (params: DriftListParams = {}) => {
    const q = new URLSearchParams()
    if (params.server_id != null) q.set('server_id', String(params.server_id))
    if (params.status != null) q.set('status', params.status)
    if (params.drifted_only) q.set('drifted_only', 'true')
    const qs = q.toString()
    return apiClient.get<DriftBaseline[]>(`/api/drift${qs ? `?${qs}` : ''}`)
  },
  summary: () => apiClient.get<DriftSummary>('/api/drift/summary'),
  get: (id: number) => apiClient.get<DriftDiff>(`/api/drift/${id}`),
  accept: (id: number, reason: string) =>
    apiClient.post<DriftBaseline>(`/api/drift/${id}/accept`, { reason }),
  runCheck: (serverId: number) =>
    apiClient.post<{ job_id: number }>(`/api/servers/${serverId}/drift_check`, {}),
}
