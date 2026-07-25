/**
 * Typed data layer for platform settings (sessions 1.12 + 6.6, CLAUDE.md rule 9):
 * white-label brand layer, platform defaults, and read-only environment facts.
 * Only Admins (settings:manage) may write; logos/favicon uploaded as base64.
 */

import { apiClient } from './client'

export interface Settings {
  product_name: string
  logo_path: string | null
  logo_dark_path: string | null
  favicon_path: string | null
  accent_hex: string | null
  support_link: string | null
  footer_line: string | null
  default_tz: string
  bench_base_path: string
  port_range_start: number
  port_range_end: number
  updated_at: string
}

/** Public brand bundle — safe to fetch without auth (login page, wizard). */
export interface Branding {
  product_name: string
  accent_hex: string | null
  logo_url: string | null
  logo_dark_url: string | null
  favicon_url: string | null
  support_link: string | null
  footer_line: string | null
}

export interface SettingsUpdate {
  product_name?: string
  accent_hex?: string | null
  support_link?: string | null
  footer_line?: string | null
  default_tz?: string
  /** Absolute path; a relative path is rejected 422 by the API. */
  bench_base_path?: string
  port_range_start?: number
  port_range_end?: number
}

export type LogoContentType = 'image/png' | 'image/jpeg' | 'image/svg+xml' | 'image/webp'
export type FaviconContentType = 'image/png' | 'image/x-icon' | 'image/vnd.microsoft.icon'

export interface LogoUploadPayload {
  content_type: LogoContentType
  content_base64: string
}

export interface FaviconUploadPayload {
  content_type: FaviconContentType
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
  /** Public brand bundle — no auth required. */
  branding: () => apiClient.get<Branding>('/api/branding'),

  get: () => apiClient.get<Settings>('/api/settings'),
  update: (payload: SettingsUpdate) => apiClient.put<Settings>('/api/settings', payload),

  uploadLogo: (payload: LogoUploadPayload) =>
    apiClient.post<Settings>('/api/settings/logo', payload),
  uploadLogoDark: (payload: LogoUploadPayload) =>
    apiClient.post<Settings>('/api/settings/logo-dark', payload),
  uploadFavicon: (payload: FaviconUploadPayload) =>
    apiClient.post<Settings>('/api/settings/favicon', payload),

  environment: () => apiClient.get<Environment>('/api/settings/environment'),

  /** Cache-bust logo/favicon URLs using settings' updated_at timestamp. */
  logoUrl: (updatedAt: string) => `/api/settings/logo?v=${encodeURIComponent(updatedAt)}`,
  logoDarkUrl: (updatedAt: string) => `/api/settings/logo-dark?v=${encodeURIComponent(updatedAt)}`,
  faviconUrl: (updatedAt: string) => `/api/settings/favicon?v=${encodeURIComponent(updatedAt)}`,
}
