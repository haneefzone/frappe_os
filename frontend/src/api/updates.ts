/**
 * Typed data layer for the safe update pipeline (FDM 3.3, CLAUDE.md rule 9).
 * Every mutating step enqueues a job; inspect via jobsApi. Promote to prod is
 * destructive and requires danger permission + typed site name + sign-off.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type PipelinePhase =
  | 'cloning'
  | 'cloned'
  | 'updating'
  | 'updated'
  | 'verifying'
  | 'verified'
  | 'promoting'
  | 'promoted'
  | 'rolled_back'
  | 'failed'

export interface ChecklistItemOut {
  key: string
  label: string
  ok: boolean
  detail: string | null
}

export interface PipelineOut {
  id: number
  source_site_id: number
  source_bench_id: number
  staging_bench_id: number
  staging_site_name: string
  staging_site_id: number | null
  phase: PipelinePhase
  scrub_method: string | null
  checklist_ok: boolean
  checklist: ChecklistItemOut[]
  pre_backup_id: number | null
  clone_job_id: number | null
  update_job_id: number | null
  verify_job_id: number | null
  promote_job_id: number | null
  rollback_job_id: number | null
  note: string | null
  created_at: string
  updated_at: string
}

export interface CreatePipelinePayload {
  source_site_id: number
  staging_bench_id?: number
  staging_site_name: string
  admin_password: string
  scrub_method?: string
  priority?: string
}

export interface PromotePayload {
  confirm_name?: string
  signoff?: string
  priority?: string
}

export interface SetEnvironmentPayload {
  environment: 'dev' | 'staging' | 'prod'
}

export const updatesApi = {
  /** Clone a prod site to a staging bench — returns the new pipeline (with clone_job_id set). */
  create: (payload: CreatePipelinePayload) =>
    apiClient.post<PipelineOut>('/api/update-pipelines', payload),

  /** Run bench update on the staging clone. */
  updateStaging: (pipelineId: number) =>
    apiClient.post<JobDetail>(`/api/update-pipelines/${pipelineId}/update-staging`),

  /** Run the four-probe verification checklist on staging. */
  verify: (pipelineId: number) =>
    apiClient.post<JobDetail>(`/api/update-pipelines/${pipelineId}/verify`, { priority: 'high' }),

  /** Promote to prod — requires danger + typed confirm_name + signoff for prod sources. */
  promote: (pipelineId: number, payload: PromotePayload) =>
    apiClient.post<JobDetail>(`/api/update-pipelines/${pipelineId}/promote`, payload),

  /** Fetch a single pipeline (for live polling). */
  get: (pipelineId: number) =>
    apiClient.get<PipelineOut>(`/api/update-pipelines/${pipelineId}`),

  /** List all pipelines. */
  list: () => apiClient.get<PipelineOut[]>('/api/update-pipelines'),

  /** Classify a site as dev | staging | prod. */
  setEnvironment: (siteId: number, payload: SetEnvironmentPayload) =>
    apiClient.post<{ id: number; environment: string }>(`/api/sites/${siteId}/environment`, payload),
}
