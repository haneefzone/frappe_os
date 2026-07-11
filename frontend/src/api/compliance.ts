/**
 * Typed data layer for backup compliance policies (session 2.3, CLAUDE.md rule 9).
 * Per-site policies (RPO / retention / offsite / restore-test) are upserted via
 * PUT; the fleet-wide compliance summary is read from /api/compliance and can be
 * recomputed on demand via POST /api/compliance/evaluate. All calls go through
 * apiClient, which adds the CSRF header on mutations automatically.
 */

import { apiClient } from './client'

export interface BackupPolicy {
  site_id: number
  /** Recovery point objective — max age of the newest backup, in hours (1..8760). */
  rpo_hours: number
  /** Keep-days floor; null = no retention requirement. */
  retention_days: number | null
  require_offsite: boolean
  require_restore_test: boolean
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface BackupPolicyPayload {
  rpo_hours: number
  retention_days: number | null
  require_offsite: boolean
  require_restore_test: boolean
  enabled: boolean
}

export type ComplianceState = 'compliant' | 'breached' | 'unknown'

export interface ComplianceBreach {
  code: string
  detail: string
}

export interface ComplianceStatus {
  site_id: number
  site_name: string
  state: ComplianceState
  last_backup_at: string | null
  breaches: ComplianceBreach[]
  evaluated_at: string | null
}

export interface ComplianceSummary {
  policied: number
  compliant: number
  breached: number
  compliance_pct: number
  statuses: ComplianceStatus[]
}

export const complianceApi = {
  /** A site's policy, or a 404 when none is set (callers tolerate the 404). */
  getPolicy: (siteId: number) => apiClient.get<BackupPolicy>(`/api/sites/${siteId}/policy`),
  /** Upsert (create or replace) a site's policy. Needs schedule:manage. */
  putPolicy: (siteId: number, payload: BackupPolicyPayload) =>
    apiClient.put<BackupPolicy>(`/api/sites/${siteId}/policy`, payload),
  /** Remove a site's policy. Needs schedule:manage. */
  deletePolicy: (siteId: number) => apiClient.delete<void>(`/api/sites/${siteId}/policy`),
  /** Fleet-wide compliance summary (read). */
  getSummary: () => apiClient.get<ComplianceSummary>('/api/compliance'),
  /** Recompute now and return the fresh summary. Needs schedule:manage. */
  evaluate: () => apiClient.post<ComplianceSummary>('/api/compliance/evaluate', {}),
}
