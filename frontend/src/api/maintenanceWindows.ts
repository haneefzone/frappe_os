/**
 * API client for maintenance windows (session 3.5).
 * Typed data layer — raw apiClient only, no frappe-ui resource semantics (CLAUDE.md rule 9).
 */

import { apiClient } from './client'

export type DangerClass = 'update' | 'restore' | 'production_setup'

export interface MaintenanceWindow {
  id: number
  name: string
  server_id: number
  cron: string
  duration_minutes: number
  timezone: string
  blocked_danger_classes: DangerClass[]
  enabled: boolean
  created_by: number | null
  created_at: string
  updated_at: string
  is_active_now: boolean
}

export interface CreateMaintenanceWindowPayload {
  name: string
  server_id: number
  cron: string
  duration_minutes?: number
  timezone?: string
  blocked_danger_classes?: DangerClass[]
  enabled?: boolean
}

export type UpdateMaintenanceWindowPayload = Partial<
  Omit<CreateMaintenanceWindowPayload, 'server_id'>
>

export const maintenanceWindowsApi = {
  list: (serverIdFilter?: number) => {
    const url = serverIdFilter
      ? `/api/maintenance-windows?server_id=${serverIdFilter}`
      : '/api/maintenance-windows'
    return apiClient.get<MaintenanceWindow[]>(url)
  },
  get: (id: number) => apiClient.get<MaintenanceWindow>(`/api/maintenance-windows/${id}`),
  create: (payload: CreateMaintenanceWindowPayload) =>
    apiClient.post<MaintenanceWindow>('/api/maintenance-windows', payload),
  update: (id: number, payload: UpdateMaintenanceWindowPayload) =>
    apiClient.patch<MaintenanceWindow>(`/api/maintenance-windows/${id}`, payload),
  remove: (id: number) => apiClient.delete<void>(`/api/maintenance-windows/${id}`),
  dangerClasses: () => apiClient.get<DangerClass[]>('/api/maintenance-windows/danger-classes'),
}

export const DANGER_CLASS_LABELS: Record<DangerClass, string> = {
  update: 'Updates',
  restore: 'Restores',
  production_setup: 'Production setup',
}
