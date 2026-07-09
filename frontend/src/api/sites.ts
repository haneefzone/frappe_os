/**
 * Typed data layer for the sites inventory (CLAUDE.md rule 9). Listing/detail
 * go through apiClient; create and the scheduler/maintenance toggles each
 * launch a job and return it, which the caller watches via the jobs API.
 */

import { apiClient } from './client'
import type { EnvTag } from './servers'
import type { JobDetail } from './jobs'

export type SiteStatus = 'active' | 'missing'
export type SiteHealth = 'unknown' | 'ok' | 'warn' | 'err'

export interface Site {
  id: number
  bench_id: number
  bench_name: string
  server_id: number
  server_name: string
  server_hostname: string
  server_env_tag: EnvTag
  name: string
  status: SiteStatus
  scheduler_enabled: boolean | null
  maintenance_mode: boolean
  health: SiteHealth
  webserver_port: number | null
  /** http://<server host>:<bench web port>, or null when the port is unknown. */
  url: string | null
  discovered_at: string | null
  created_at: string
  updated_at: string
}

export interface CreateSitePayload {
  bench_id: number
  name: string
  admin_password: string
  priority?: 'high' | 'default' | 'low'
}

export const sitesApi = {
  list: (bench?: number) =>
    apiClient.get<Site[]>(`/api/sites${bench != null ? `?bench=${bench}` : ''}`),
  get: (id: number) => apiClient.get<Site>(`/api/sites/${id}`),
  create: (payload: CreateSitePayload) => apiClient.post<JobDetail>('/api/sites', payload),
  setScheduler: (id: number, enabled: boolean) =>
    apiClient.post<JobDetail>(`/api/sites/${id}/scheduler`, { enabled }),
  setMaintenance: (id: number, enabled: boolean) =>
    apiClient.post<JobDetail>(`/api/sites/${id}/maintenance`, { enabled }),
}
