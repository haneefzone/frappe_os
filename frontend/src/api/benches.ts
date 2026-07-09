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

// --- Guided create (session 1.7) ----------------------------------------- //

/** One row of the Frappe version matrix, driving the wizard's radio cards. */
export interface VersionMatrixEntry {
  major: string
  branch: string
  python: string
  node: string
  mariadb: string
  tooling: string
  /** Pre-rendered matrix line, e.g. "Python 3.14 · Node 24 · MariaDB 11.8". */
  line: string
}

export interface VersionMatrix {
  entries: VersionMatrixEntry[]
}

export type PreflightStatus = 'pass' | 'warn' | 'fail'

/** One pre-flight probe's outcome (mirrors backend CheckResult). */
export interface PreflightCheck {
  key: string
  title: string
  status: PreflightStatus
  detail: string
  blocking: boolean
}

export interface PreflightReport {
  blocked: boolean
  has_warnings: boolean
  checks: PreflightCheck[]
}

export interface PreflightPayload {
  server_id: number
  frappe_version: string
  path?: string
}

export interface CreateBenchPayload {
  server_id: number
  frappe_version: string
  name: string
  path?: string
}

/** The `PREFLIGHT_RESULT {json}` marker the pre-flight job emits on its log. */
const PREFLIGHT_MARKER = 'PREFLIGHT_RESULT '

/** Parse a preflight report out of a job log line, or null if it isn't one. */
export function parsePreflightLine(content: string): PreflightReport | null {
  const i = content.indexOf(PREFLIGHT_MARKER)
  if (i === -1) return null
  try {
    return JSON.parse(content.slice(i + PREFLIGHT_MARKER.length)) as PreflightReport
  } catch {
    return null
  }
}

export const benchesApi = {
  list: (server?: number) =>
    apiClient.get<Bench[]>(`/api/benches${server != null ? `?server=${server}` : ''}`),
  get: (id: number) => apiClient.get<Bench>(`/api/benches/${id}`),
  discover: (serverId: number, payload: DiscoverPayload = {}) =>
    apiClient.post<JobDetail>(`/api/servers/${serverId}/discover-benches`, payload),
  versionMatrix: () => apiClient.get<VersionMatrix>('/api/benches/version-matrix'),
  preflight: (payload: PreflightPayload) =>
    apiClient.post<JobDetail>('/api/benches/preflight', payload),
  create: (payload: CreateBenchPayload) => apiClient.post<JobDetail>('/api/benches', payload),
}
