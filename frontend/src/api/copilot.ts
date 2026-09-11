/**
 * Typed data layer for the panel copilot (CLAUDE.md rule 9, uiux-spec §17):
 * "Ask AI to analyze" on failed jobs and the natural-language command palette.
 * Everything rides through apiClient — the backend owns the sanitisation, the
 * model routing, and (for palette proposals) the exact run.method/path/body, so
 * the frontend never builds a command or shell string of its own.
 */

import { apiClient } from './client'

export type AnalysisStatus = 'pending' | 'running' | 'success' | 'failure'

export interface JobAnalysis {
  id: number
  job_id: number
  status: AnalysisStatus
  model: string | null
  root_cause: string | null
  suggested_fix: string | null
  summary: string | null
  error: string | null
  requested_by: number | null
  created_at: string
  completed_at: string | null
}

/** One NL-palette proposal. `run` is the server-authored call to execute on confirm. */
export interface NLProposal {
  title: string
  action_name: string
  summary: string
  site_id: number
  site_name: string
  params: Record<string, string>
  allowed: boolean
  confirm: boolean
  run: {
    method: string
    path: string
    body: Record<string, unknown>
  }
}

export interface NLResolveResponse {
  resolved: boolean
  intent: string | null
  reason: string | null
  proposals: NLProposal[]
}

export const copilotApi = {
  analyzeJob: (jobId: number) => apiClient.post<JobAnalysis>(`/api/jobs/${jobId}/analyze`),
  getAnalysis: (jobId: number) => apiClient.get<JobAnalysis>(`/api/jobs/${jobId}/analyze`),
  resolvePalette: (q: string) => apiClient.post<NLResolveResponse>('/api/palette/resolve', { q }),
}
