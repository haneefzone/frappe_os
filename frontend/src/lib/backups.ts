/** Shared presentation helpers for the backups inventory (session 1.11). */

import type { Status } from '../components/types'
import type { ArtifactKind, Backup, BackupStatus } from '../api/backups'

/** Backup lifecycle -> StatusDot color. */
export function backupStatusDot(status: BackupStatus): Status {
  switch (status) {
    case 'success':
      return 'ok'
    case 'failed':
      return 'err'
    default:
      return 'running'
  }
}

export const BACKUP_STATUS_LABEL: Record<BackupStatus, string> = {
  success: 'Complete',
  failed: 'Failed',
  pending: 'In progress',
}

export const BACKUP_TYPE_LABEL: Record<string, string> = {
  db: 'Database only',
  'with-files': 'With files',
}

export const ARTIFACT_LABEL: Record<ArtifactKind | string, string> = {
  database: 'Database',
  public_files: 'Public files',
  private_files: 'Private files',
  config: 'Site config',
}

/** Human-readable byte size (binary units). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  const value = bytes / 1024 ** i
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

/** Total size across a list of backups (for the KPI header). */
export function totalSize(backups: Backup[]): number {
  return backups.reduce((sum, b) => sum + (b.size_bytes ?? 0), 0)
}
