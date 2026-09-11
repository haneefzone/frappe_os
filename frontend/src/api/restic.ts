/**
 * Typed data layer for restic config-tier DR repos (sessions 4.1 + 4.2).
 * Read endpoints are visible to every role. Mutations (configure, forget,
 * check) require server:manage.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export interface ResticRepo {
  kind: 'config'
  id: number
  server_id: number
  storage_target_id: number | null
  storage_target_name: string | null
  prefix: string
  password_set: boolean
  initialized: boolean
  last_backup_at: string | null
  last_snapshot_id: string | null
  last_check_at: string | null
  // 4.2 evidence
  last_check_ok: boolean | null
  last_check_summary: string | null
  last_forget_at: string | null
  retention_keep_last: number | null
  retention_keep_daily: number | null
  retention_keep_weekly: number | null
  retention_keep_monthly: number | null
  retention_summary: string | null
  created_at: string
  updated_at: string
}

export interface ResticRepoConfigure {
  storage_target_id: number
  prefix?: string
  password?: string
  retention_keep_last?: number | null
  retention_keep_daily?: number | null
  retention_keep_weekly?: number | null
  retention_keep_monthly?: number | null
}

export const resticApi = {
  list: () => apiClient.get<ResticRepo[]>('/api/restic-repos'),
  get: (serverId: number) => apiClient.get<ResticRepo>(`/api/servers/${serverId}/restic-repo`),
  configure: (serverId: number, body: ResticRepoConfigure) =>
    apiClient.put<ResticRepo>(`/api/servers/${serverId}/restic-repo`, body),
  check: (serverId: number) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/restic-repo/check`),
  forget: (serverId: number) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/restic-repo/forget`),
}
