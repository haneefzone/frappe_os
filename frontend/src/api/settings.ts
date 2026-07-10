/**
 * Typed data layer for platform settings (session 1.12, CLAUDE.md rule 9):
 * white-label branding, platform defaults, and read-only environment facts.
 * Only Admins (settings:manage) may write; the logo is uploaded as base64.
 */

import { apiClient } from './client'

export interface Settings {
  product_name: string
  logo_path: string | null
  default_tz: string
  bench_base_path: string
  port_range_start: number
  port_range_end: number
  updated_at: string
}

export interface SettingsUpdate {
  product_name?: string
  default_tz?: string
  /** Absolute path; a relative path is rejected 422 by the API. */
  bench_base_path?: string
  port_range_start?: number
  port_range_end?: number
}

export type LogoContentType = 'image/png' | 'image/jpeg' | 'image/svg+xml' | 'image/webp'

export interface LogoUploadPayload {
  content_type: LogoContentType
  content_base64: string
}

export interface Environment {
  app_version: string
  python_version: string
  platform: string
  database_backend: string
  redis_configured: boolean
  debug: boolean
  default_tz: string
  monitoring_enabled: boolean
  monitoring_interval_seconds: number
}

export const settingsApi = {
  get: () => apiClient.get<Settings>('/api/settings'),
  update: (payload: SettingsUpdate) => apiClient.put<Settings>('/api/settings', payload),
  uploadLogo: (payload: LogoUploadPayload) =>
    apiClient.post<Settings>('/api/settings/logo', payload),
  environment: () => apiClient.get<Environment>('/api/settings/environment'),
  /** Raw logo image URL, cache-busted by the settings' updated_at. */
  logoUrl: (updatedAt: string) => `/api/settings/logo?v=${encodeURIComponent(updatedAt)}`,
}
