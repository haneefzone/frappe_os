/**
 * Typed data layer for the update advisor (session 3.2, CLAUDE.md rule 9).
 * Read endpoints compare each installed app's ref against its upstream and
 * report how far behind it is; refresh launches a background re-check job.
 */

import { apiClient } from './client'

export interface UpdateStatus {
  installed_app_id: number
  site_id: number
  site_name: string
  bench_id: number
  bench_name: string
  app_name: string
  branch: string | null
  installed_ref: string | null
  latest_ref: string | null
  /** null = not trackable / not yet checked; 0 = up to date; N>0 = N releases behind. */
  behind_by: number | null
  security_update: boolean
  checked_at: string | null
  last_error: string | null
}

export interface UpdatesSummary {
  apps_behind: number
  sites_behind: number
  security_updates: number
  tracked: number
  up_to_date_fraction: number
}

export interface ChangelogRelease {
  tag: string
  version: string
  notes_url: string | null
}

export interface ChangelogPreview {
  installed_app_id: number
  app_name: string
  repo_key: string | null
  installed_ref: string | null
  latest_ref: string | null
  behind_by: number | null
  /** Newest-first. */
  releases: ChangelogRelease[]
  compare_url: string | null
  releases_url: string | null
}

export interface RefreshResult {
  rq_job_id: string
}

export interface ListUpdatesParams {
  bench?: number
  site?: number
  behind_only?: boolean
}

function buildQuery(params: ListUpdatesParams): string {
  const parts: string[] = []
  if (params.bench != null) parts.push(`bench=${params.bench}`)
  if (params.site != null) parts.push(`site=${params.site}`)
  if (params.behind_only) parts.push('behind_only=true')
  return parts.length ? `?${parts.join('&')}` : ''
}

export const updatesApi = {
  list: (params: ListUpdatesParams = {}) =>
    apiClient.get<UpdateStatus[]>(`/api/updates${buildQuery(params)}`),
  summary: () => apiClient.get<UpdatesSummary>('/api/updates/summary'),
  changelog: (installedAppId: number) =>
    apiClient.get<ChangelogPreview>(`/api/updates/${installedAppId}/changelog`),
  /** Requires app:manage; may 403 for Operator/Read-only. */
  refresh: () => apiClient.post<RefreshResult>('/api/updates/refresh'),
}
