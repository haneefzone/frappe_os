/**
 * Typed data layer for the platform self-backup + master-key escrow (session 6.3).
 * All endpoints are Admin-only on the backend; the frontend guards with canManage.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type VerifyStatus = 'unverified' | 'verified' | 'failed'

export interface PlatformBackup {
  id: number
  status: string
  size_bytes: number | null
  sha256: string | null
  plaintext_sha256: string | null
  encrypted: boolean
  kdf_salt: string | null
  storage_target_id: number | null
  storage_target_name: string | null
  object_key: string | null
  verify_status: VerifyStatus
  verified_at: string | null
  restore_tested_at: string | null
  error: string | null
  taken_by_job_id: number | null
  verified_by_job_id: number | null
  created_at: string
  updated_at: string
}

export interface EscrowStatus {
  confirmed: boolean
  confirmed_at: string | null
  confirmed_by: number | null
  confirmed_by_email: string | null
}

export interface PresignedDownload {
  url: string
  expires_in: number
  filename: string
}

export interface RunBackupPayload {
  storage_target_id?: number | null
  priority?: string
}

export const platformApi = {
  listBackups: () => apiClient.get<PlatformBackup[]>('/api/platform/backups'),
  runBackup: (payload?: RunBackupPayload) =>
    apiClient.post<JobDetail>('/api/platform/backups', payload ?? {}),
  verifyBackup: (id: number) => apiClient.post<JobDetail>(`/api/platform/backups/${id}/verify`),
  downloadBackup: (id: number) =>
    apiClient.get<PresignedDownload>(`/api/platform/backups/${id}/download`),
  getEscrow: () => apiClient.get<EscrowStatus>('/api/platform/escrow'),
  confirmEscrow: () =>
    apiClient.post<EscrowStatus>('/api/platform/escrow/confirm', { acknowledge: true }),
}
