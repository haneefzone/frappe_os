/**
 * Typed data layer for the operator dashboard (session 1.12, CLAUDE.md rule 9).
 * One read endpoint that aggregates fleet health, backup compliance, running
 * jobs, and the per-server resource strip; the page polls it while mounted.
 */

import { apiClient } from './client'
import type { EnvTag, ServerStatus } from './servers'

export interface DashboardOnboarding {
  has_servers: boolean
}

export interface DashboardKpis {
  fleet_health_pct: number
  sites_up: number
  sites_total: number
  backups_24h: number
  failed_jobs_24h: number
  backup_compliance_pct: number
  /** Sites passing their backup policy / sites with a policy (session 2.3). */
  sites_compliant: number
  sites_policied: number
  /** Real 30-day fleet uptime from external HTTP checks (session 2.7). */
  uptime_30d_pct: number
  /** Count of SSL certs expiring within 30 days (session 2.4). */
  ssl_expiring_30d: number
}

export interface DashboardServer {
  id: number
  name: string
  env_tag: EnvTag
  status: ServerStatus
  cpu_pct: number | null
  mem_pct: number | null
  disk_pct: number | null
  sample_ts: string | null
}

export interface BackupGridDay {
  /** YYYY-MM-DD. */
  date: string
  success: number
  failed: number
}

export interface DashboardRunningJob {
  id: number
  action_name: string
  status: string
  target_id: string | null
  server_id: number
  created_at: string
}

export interface DriftArtifactEntry {
  id: number
  server_id: number
  artifact_key: string
  path: string
  drift_detected_at: string | null
}

export interface NeedsAttentionConfigDrift {
  count: number
  server_ids: number[]
  artifacts: DriftArtifactEntry[]
}

export interface DashboardNeedsAttention {
  config_drift: NeedsAttentionConfigDrift
}

export interface Dashboard {
  generated_at: string
  onboarding: DashboardOnboarding
  kpis: DashboardKpis
  morning_brief: string
  servers: DashboardServer[]
  /** 7 entries, oldest → newest. */
  backup_grid: BackupGridDay[]
  running_jobs: DashboardRunningJob[]
  needs_attention: DashboardNeedsAttention
}

export const dashboardApi = {
  get: () => apiClient.get<Dashboard>('/api/dashboard'),
}
