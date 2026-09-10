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
export type SiteEnvironment = 'dev' | 'staging' | 'prod'

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
  /** The site's own environment classification (dev/staging/prod). Drives the
   *  prod-update guardrail. Operators set this here or at creation time. */
  environment: SiteEnvironment
  webserver_port: number | null
  /** http://<server host>:<bench web port>, or null when the port is unknown. */
  url: string | null
  /** External HTTP uptime checking (session 2.7). */
  uptime_enabled: boolean
  check_url: string | null
  discovered_at: string | null
  created_at: string
  updated_at: string
}

/** One external HTTP probe point (for the response-time sparkline). */
export interface UptimeSample {
  ts: string
  up: boolean
  status_code: number | null
  latency_ms: number | null
  error: string | null
}

export interface UptimeSummary {
  uptime_24h_pct: number | null
  uptime_30d_pct: number | null
  samples_24h: number
  samples_30d: number
  currently_up: boolean | null
  last_status_code: number | null
  last_latency_ms: number | null
  last_checked_at: string | null
}

export interface UptimeSeries {
  site_id: number
  enabled: boolean
  check_url: string | null
  summary: UptimeSummary
  samples: UptimeSample[]
}

export interface UptimeConfigPayload {
  enabled?: boolean
  check_url?: string | null
}

export interface CreateSitePayload {
  bench_id: number
  name: string
  admin_password: string
  /** Environment classification applied at registration time (DOO-988). */
  environment?: SiteEnvironment
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
  // Maintenance actions (session 1.10) — each launches a job the caller opens.
  migrate: (id: number) => apiClient.post<JobDetail>(`/api/sites/${id}/migrate`, {}),
  clearCache: (id: number) => apiClient.post<JobDetail>(`/api/sites/${id}/clear-cache`, {}),
  clearWebsiteCache: (id: number) =>
    apiClient.post<JobDetail>(`/api/sites/${id}/clear-website-cache`, {}),
  // Uptime (session 2.7): read the series + summary; toggle the checker / set a URL.
  uptime: (id: number, hours = 24) =>
    apiClient.get<UptimeSeries>(`/api/sites/${id}/uptime?hours=${hours}`),
  setUptimeConfig: (id: number, payload: UptimeConfigPayload) =>
    apiClient.post<Site>(`/api/sites/${id}/uptime-config`, payload),
}
