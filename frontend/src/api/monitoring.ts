/**
 * Typed data layer for server monitoring (session 1.12, CLAUDE.md rule 9).
 * A read endpoint returns the latest sample plus a window of samples; the
 * restart endpoint enqueues a job (same JobDetail shape as every mutation).
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type ServiceName = 'nginx' | 'mariadb' | 'redis-server' | 'supervisor'
export type ServiceState = 'active' | 'inactive' | 'failed' | 'unknown'

export type ServiceMap = Record<string, ServiceState>

export interface MonitoringSample {
  id: number
  server_id: number
  ts: string
  ok: boolean
  error: string | null
  cpu_pct: number | null
  mem_pct: number | null
  disk_pct: number | null
  mem_used_mb: number | null
  mem_total_mb: number | null
  disk_used_gb: number | null
  disk_total_gb: number | null
  load1: number | null
  services: ServiceMap
}

export interface MonitoringResponse {
  server_id: number
  latest: MonitoringSample | null
  samples: MonitoringSample[]
}

/** The four services the platform can restart, in display order. */
export const SERVICES: { key: ServiceName; label: string }[] = [
  { key: 'nginx', label: 'nginx' },
  { key: 'mariadb', label: 'mariadb' },
  { key: 'redis-server', label: 'redis-server' },
  { key: 'supervisor', label: 'supervisor' },
]

export const monitoringApi = {
  get: (serverId: number, hours = 24) =>
    apiClient.get<MonitoringResponse>(`/api/servers/${serverId}/monitoring?hours=${hours}`),
  restartService: (serverId: number, service: ServiceName) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/services/${service}/restart`),
}
