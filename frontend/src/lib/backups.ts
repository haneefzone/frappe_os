/** Shared presentation helpers for the backups inventory (session 1.11). */

import type { Status } from '../components/types'
import type { ArtifactKind, Backup, BackupStatus, StorageState } from '../api/backups'

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

/**
 * The storage chip shown per backup row (B4.6): a backup lives on the source
 * server (local) until an offsite upload lands (offsite), with in-flight
 * (uploading) and failed states surfaced distinctly.
 */
export function storageChip(state: StorageState | string): { label: string; status: Status } {
  switch (state) {
    case 'offsite':
      return { label: 'S3', status: 'ok' }
    case 'uploading':
      return { label: 'Uploading', status: 'running' }
    case 'failed':
      return { label: 'Upload failed', status: 'err' }
    default:
      return { label: 'Local', status: 'muted' }
  }
}

export const ARTIFACT_LABEL: Record<ArtifactKind | string, string> = {
  database: 'Database',
  public_files: 'Public files',
  private_files: 'Private files',
  config: 'Site config',
}

/**
 * Kind chip — distinguishes config-tier restic repos (`config`) from the
 * 1.11 site backups (`site`) in the §6 backup-evidence view.
 */
export function kindChip(kind: string): { label: string; status: Status } {
  return kind === 'config'
    ? { label: 'Config', status: 'running' }
    : { label: 'Site', status: 'muted' }
}

/**
 * Storage chip for restic repos: shows the attached S3 target name when set,
 * or a muted "No target" placeholder while a repo is not yet configured.
 */
export function resticStorageChip(targetName: string | null): { label: string; status: Status } {
  return targetName ? { label: targetName, status: 'ok' } : { label: 'No target', status: 'muted' }
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
