import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type ToolStatus = 'ok' | 'outdated' | 'missing' | 'unknown'

export interface ToolOut {
  tool_id: string
  display_name: string
  group: string
  detected_version: string | null
  recommended_version: string | null
  status: ToolStatus
  last_checked_at: string | null
  installable: boolean
  needs_root: boolean
  critical: boolean
  note: string
}

export interface ToolGroupOut {
  group: string
  tools: ToolOut[]
  ok_count: number
  total_count: number
}

export interface ServerToolsOut {
  server_id: number
  /** Frappe major the recommendations were resolved against; null when no bench yet. */
  frappe_major: string | null
  /** Null until the first scan — drives the EmptyState. */
  last_scanned_at: string | null
  groups: ToolGroupOut[]
}

export const toolsApi = {
  list: (serverId: number) =>
    apiClient.get<ServerToolsOut>(`/api/servers/${serverId}/tools`),
  scan: (serverId: number) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/tools/scan`),
  install: (serverId: number, toolId: string) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/tools/${toolId}/install`),
}
