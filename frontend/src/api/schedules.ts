/**
 * Typed data layer for recurring schedules (session 2.1, CLAUDE.md rule 9).
 * Listing/CRUD go through apiClient; run-now enqueues a job the caller watches
 * via the jobs API.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type ScheduleActionName = 'site.backup' | 'backup.retention_sweep'
export type SchedulePriority = 'high' | 'default' | 'low'

export interface Schedule {
  id: number
  name: string
  target_type: string
  target_id: number
  target_label: string | null
  action_name: ScheduleActionName | string
  cron: string | null
  interval_seconds: number | null
  timezone: string
  priority: SchedulePriority
  with_files: boolean
  retention_keep_last: number | null
  retention_keep_days: number | null
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_run_job_id: number | null
  last_run_status: string | null
  created_at: string
  updated_at: string
}

export interface CreateSchedulePayload {
  name: string
  target_type?: string
  target_id: number
  action_name: ScheduleActionName | string
  cron?: string | null
  interval_seconds?: number | null
  timezone?: string
  priority?: SchedulePriority
  with_files?: boolean
  retention_keep_last?: number | null
  retention_keep_days?: number | null
}

export type UpdateSchedulePayload = Partial<
  Omit<CreateSchedulePayload, 'target_id' | 'target_type' | 'action_name'>
> & { enabled?: boolean; clear_keep_last?: boolean; clear_keep_days?: boolean }

export const schedulesApi = {
  list: () => apiClient.get<Schedule[]>('/api/schedules'),
  get: (id: number) => apiClient.get<Schedule>(`/api/schedules/${id}`),
  create: (payload: CreateSchedulePayload) =>
    apiClient.post<Schedule>('/api/schedules', payload),
  update: (id: number, payload: UpdateSchedulePayload) =>
    apiClient.patch<Schedule>(`/api/schedules/${id}`, payload),
  setEnabled: (id: number, enabled: boolean) =>
    apiClient.post<Schedule>(`/api/schedules/${id}/enabled`, { enabled }),
  remove: (id: number) => apiClient.delete<void>(`/api/schedules/${id}`),
  runNow: (id: number) => apiClient.post<JobDetail>(`/api/schedules/${id}/run-now`, {}),
}

/** Human-readable cadence for the list ("Daily at 02:00", "Every 6h"). */
export function cadenceLabel(s: Pick<Schedule, 'cron' | 'interval_seconds' | 'timezone'>): string {
  if (s.interval_seconds != null) {
    const secs = s.interval_seconds
    if (secs % 86400 === 0) return `Every ${secs / 86400}d`
    if (secs % 3600 === 0) return `Every ${secs / 3600}h`
    if (secs % 60 === 0) return `Every ${secs / 60}m`
    return `Every ${secs}s`
  }
  if (s.cron) return `${s.cron} (${s.timezone})`
  return '—'
}
