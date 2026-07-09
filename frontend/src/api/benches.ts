/**
 * Typed data layer for the bench inventory (CLAUDE.md rule 9). Listing and
 * detail go through apiClient; discovery launches a `bench.discover` job and
 * returns it, which the caller then watches via the jobs API.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type BenchStatus = 'active' | 'missing'

export interface BenchPorts {
  webserver_port: number | null
  socketio_port: number | null
  redis_cache_port: number | null
  redis_queue_port: number | null
  redis_socketio_port: number | null
  file_watcher_port: number | null
}

export interface Bench {
  id: number
  server_id: number
  name: string
  path: string
  frappe_version: string | null
  python_version: string | null
  node_version: string | null
  ports: BenchPorts
  is_production: boolean
  status: BenchStatus
  discovered_at: string | null
  created_at: string
  updated_at: string
}

export interface DiscoverPayload {
  base_paths?: string[]
  priority?: 'high' | 'default' | 'low'
}

export const benchesApi = {
  list: (server?: number) =>
    apiClient.get<Bench[]>(`/api/benches${server != null ? `?server=${server}` : ''}`),
  get: (id: number) => apiClient.get<Bench>(`/api/benches/${id}`),
  discover: (serverId: number, payload: DiscoverPayload = {}) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/discover-benches`, payload),
}
