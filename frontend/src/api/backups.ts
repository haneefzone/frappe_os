/**
 * Typed data layer for backups + guided restore (session 1.11, CLAUDE.md rule 9).
 * Listing/detail/compatibility go through apiClient; creating a backup, validating
 * one, and launching a restore each enqueue a job the caller watches via the jobs
 * API. Downloads stream straight from the API (a plain anchor href), so they are
 * not modelled here as a fetch.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type BackupType = 'db' | 'with-files'
export type BackupStatus = 'pending' | 'success' | 'failed'
export type ArtifactKind = 'database' | 'public_files' | 'private_files' | 'config'
export type RestoreMode = 'same_site' | 'new_site' | 'different_bench'

export interface BackupArtifact {
  kind: ArtifactKind | string
  size_bytes: number
  checksum_sha256: string
}

export interface Backup {
  id: number
  site_id: number
  site_name: string
  bench_id: number
  bench_name: string
  server_id: number
  type: BackupType
  status: BackupStatus
  size_bytes: number | null
  artifacts: BackupArtifact[]
  available_artifacts: string[]
  frappe_version: string | null
  restore_tested: boolean
  taken_by_job_id: number | null
  created_at: string
  updated_at: string
}

export interface BackupFilters {
  site?: number
  bench?: number
  type?: BackupType
  restore_tested?: boolean
}

export interface CreateBackupPayload {
  with_files: boolean
  priority?: 'high' | 'default' | 'low'
}

export interface RestorePayload {
  mode: RestoreMode
  backup_id: number
  target_bench_id?: number
  target_site_name?: string
  admin_password?: string
  confirm_name?: string
  priority?: 'high' | 'default' | 'low'
}

export interface Compatibility {
  ok: boolean
  source_major: number | null
  target_major: number | null
  reason: string
}

function query(filters?: BackupFilters): string {
  if (!filters) return ''
  const q = new URLSearchParams()
  if (filters.site != null) q.set('site', String(filters.site))
  if (filters.bench != null) q.set('bench', String(filters.bench))
  if (filters.type) q.set('type', filters.type)
  if (filters.restore_tested != null) q.set('restore_tested', String(filters.restore_tested))
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const backupsApi = {
  list: (filters?: BackupFilters) => apiClient.get<Backup[]>(`/api/backups${query(filters)}`),
  get: (id: number) => apiClient.get<Backup>(`/api/backups/${id}`),
  create: (siteId: number, payload: CreateBackupPayload) =>
    apiClient.post<JobDetail>(`/api/sites/${siteId}/backups`, payload),
  validate: (id: number) => apiClient.post<JobDetail>(`/api/backups/${id}/validate`),
  compatibility: (backupId: number, benchId: number) =>
    apiClient.get<Compatibility>(
      `/api/restores/compatibility?backup_id=${backupId}&bench_id=${benchId}`,
    ),
  restore: (payload: RestorePayload) => apiClient.post<JobDetail>('/api/restores', payload),
  /** Direct download URL for one artifact (opened as an anchor href). */
  downloadUrl: (id: number, artifact: string) =>
    `/api/backups/${id}/download?artifact=${encodeURIComponent(artifact)}`,
}

/** Parse the `VALIDATE_RESULT <json>` line the validate job emits. */
export interface ValidateResult {
  backup_id: number
  type: string
  frappe_version: string | null
  size_bytes: number | null
  artifact_count: number
  all_ok: boolean
  artifacts: { kind: string; ok: boolean; missing: boolean }[]
}

export function parseValidateLine(content: string): ValidateResult | null {
  const marker = 'VALIDATE_RESULT '
  if (!content.startsWith(marker)) return null
  try {
    return JSON.parse(content.slice(marker.length)) as ValidateResult
  } catch {
    return null
  }
}
