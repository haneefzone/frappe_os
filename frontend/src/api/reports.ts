/**
 * Typed data layer for the reports suite (session 6.2, uiux-spec B4.16).
 * All calls go through apiClient (CLAUDE.md rule 9). Report runs are async
 * jobs by default; the ?sync=true path is used for small bounded CSV exports.
 */

import { apiClient } from './client'

export interface ReportParam {
  name: string
  kind: string
  label: string
  required: boolean
  default: number | string | null
  enum: string[] | null
  min: number | null
  max: number | null
}

export interface Report {
  id: string
  title: string
  description: string
  required_permission: string
  evidence: boolean
  params: ReportParam[]
}

export interface ReportRun {
  id: number
  report_id: string
  format: string
  status: string
  params: Record<string, unknown>
  row_count: number | null
  artifact_bytes: number | null
  sha256: string | null
  error: string | null
  requested_by: number | null
  job_id: number | null
  created_at: string | null
  completed_at: string | null
  download_url: string | null
}

export interface RunRequest {
  format: 'csv' | 'pdf'
  params: Record<string, unknown>
  recipients?: string
}

/** Response from the async job path (202). */
export interface AsyncRunResponse {
  job_id: number
  report_id: string
}

export const reportsApi = {
  list: () => apiClient.get<Report[]>('/api/reports'),

  /** Async run — enqueues a job and returns 202 with job_id. */
  run: (reportId: string, body: RunRequest) =>
    apiClient.post<AsyncRunResponse>(`/api/reports/${reportId}/run`, body),

  /** Synchronous CSV run — renders inline and returns the completed ReportRun. */
  runSync: (reportId: string, body: Pick<RunRequest, 'params'>) =>
    apiClient.post<ReportRun>(`/api/reports/${reportId}/run?sync=true`, {
      format: 'csv',
      ...body,
    }),

  listRuns: (reportId?: string, limit = 100) =>
    apiClient.get<ReportRun[]>(
      `/api/report-runs${reportId ? `?report_id=${encodeURIComponent(reportId)}&limit=${limit}` : `?limit=${limit}`}`,
    ),
}

/** Human-readable file size. */
export function formatBytes(bytes: number | null): string {
  if (bytes === null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
