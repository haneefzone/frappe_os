/**
 * Typed data layer for restic config-tier DR repos (session 4.1).
 * Both endpoints are READ-visible so the §6 backup-evidence view renders
 * the kind/storage chip for every role. Mutations (configure, install,
 * init, backup, snapshots) live on the backend; there is no mutate
 * surface here — reads only.
 */

import { apiClient } from './client'

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
  last_check_at: string | null
  last_snapshot_id: string | null
  created_at: string
  updated_at: string
}

export const resticApi = {
  list: () => apiClient.get<ResticRepo[]>('/api/restic-repos'),
  get: (serverId: number) => apiClient.get<ResticRepo>(`/api/servers/${serverId}/restic-repo`),
}
