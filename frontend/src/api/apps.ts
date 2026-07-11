/**
 * Typed data layer for app sources + installs (session 1.9, CLAUDE.md rule 9).
 * Source CRUD and the installed-app matrix go through apiClient; install,
 * uninstall and branch-listing each launch a job the caller watches via the
 * jobs API. The deploy key is write-only — responses expose only
 * `has_deploy_key`.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type AppSourceKind = 'marketplace' | 'github' | 'gitlab' | string

export interface AppSource {
  id: number
  name: string
  repo_url: string
  kind: AppSourceKind
  default_branch: string | null
  is_private: boolean
  has_deploy_key: boolean
  notes: string | null
  created_at: string
  updated_at: string
}

export interface CreateAppSourcePayload {
  name: string
  repo_url: string
  default_branch?: string | null
  is_private?: boolean
  deploy_key?: string | null
  notes?: string | null
}

export type UpdateAppSourcePayload = Partial<CreateAppSourcePayload>

export interface InstalledApp {
  id: number
  site_id: number
  site_name: string
  bench_id: number
  bench_name: string
  server_id: number
  app_source_id: number | null
  app_name: string
  branch: string | null
  version: string | null
  installed_at: string | null
  /** Update advisor (session 3.2): how many releases behind, if tracked. */
  behind_by?: number | null
  latest_ref?: string | null
  security_update?: boolean
  update_checked_at?: string | null
}

export interface InstallAppPayload {
  /** Module name to install; defaults from the source when omitted. */
  app?: string
  app_source_id?: number
  /** Ad-hoc repo URL / marketplace name (when no saved source is used). */
  source?: string
  branch?: string
  priority?: 'high' | 'default' | 'low'
}

export interface ListBranchesPayload {
  server_id: number
  repo_url?: string
  app_source_id?: number
}

export const appsApi = {
  listSources: () => apiClient.get<AppSource[]>('/api/app-sources'),
  getSource: (id: number) => apiClient.get<AppSource>(`/api/app-sources/${id}`),
  createSource: (payload: CreateAppSourcePayload) =>
    apiClient.post<AppSource>('/api/app-sources', payload),
  updateSource: (id: number, payload: UpdateAppSourcePayload) =>
    apiClient.patch<AppSource>(`/api/app-sources/${id}`, payload),
  deleteSource: (id: number) => apiClient.delete<void>(`/api/app-sources/${id}`),

  /** Launch a `git ls-remote` job; tail it for the `BRANCHES_RESULT` line. */
  listBranches: (payload: ListBranchesPayload) =>
    apiClient.post<JobDetail>('/api/app-sources/branches', payload),

  listInstalled: (bench?: number) =>
    apiClient.get<InstalledApp[]>(`/api/installed-apps${bench != null ? `?bench=${bench}` : ''}`),

  install: (siteId: number, payload: InstallAppPayload) =>
    apiClient.post<JobDetail>(`/api/sites/${siteId}/apps`, payload),
  uninstall: (siteId: number, appName: string, confirmName: string) =>
    apiClient.delete<JobDetail>(`/api/sites/${siteId}/apps/${encodeURIComponent(appName)}`, {
      confirm_name: confirmName,
    }),
}

/** Parse the `BRANCHES_RESULT <json>` line the list-branches job emits. */
export function parseBranchesLine(content: string): string[] | null {
  const marker = 'BRANCHES_RESULT '
  if (!content.startsWith(marker)) return null
  try {
    const parsed = JSON.parse(content.slice(marker.length))
    return Array.isArray(parsed) ? (parsed as string[]) : null
  } catch {
    return null
  }
}
