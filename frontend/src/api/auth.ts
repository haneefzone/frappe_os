/** Typed data layer for 2FA, session management, and security policy (DOO-258, session 6.5). */

import { apiClient } from './client'

export interface TOTPSetupOut {
  secret: string
  provisioning_uri: string
}

export interface TOTPConfirmOut {
  recovery_codes: string[]
}

export interface SessionOut {
  id: number
  created_at: string
  last_seen_at: string
  ip: string | null
  user_agent: string | null
  is_current: boolean
}

export interface LoginAttemptOut {
  id: number
  email: string
  ip: string | null
  success: boolean
  reason: string
  created_at: string
}

export interface SecurityPolicyOut {
  enforce_2fa_roles: string[]
  password_min_length: number
  password_require_complexity: boolean
  password_reuse_history: number
  ip_allowlist: string[]
  session_idle_timeout_minutes: number
  session_absolute_timeout_minutes: number
  updated_at: string
}

export interface SecurityPolicyUpdate {
  enforce_2fa_roles?: string[]
  password_min_length?: number
  password_require_complexity?: boolean
  password_reuse_history?: number
  ip_allowlist?: string[]
  session_idle_timeout_minutes?: number
  session_absolute_timeout_minutes?: number
}

export const mfaApi = {
  /** Begin TOTP setup — returns the secret and provisioning URI (shown once). */
  setup: () => apiClient.post<TOTPSetupOut>('/api/auth/2fa/setup'),
  /** Confirm TOTP enrolment by verifying a code; returns the one-time recovery codes. */
  confirm: (code: string) => apiClient.post<TOTPConfirmOut>('/api/auth/2fa/confirm', { code }),
  /** Disable TOTP for the current user. */
  disable: () => apiClient.post<void>('/api/auth/2fa/disable'),
  /** Exchange an mfa_pending cookie + TOTP/recovery code for a real session. */
  verify: (code: string) => apiClient.post<import('./client').UserInfo>('/api/auth/2fa/verify', { code }),
}

export const sessionsApi = {
  list: () => apiClient.get<SessionOut[]>('/api/auth/sessions'),
  revoke: (id: number) => apiClient.post<void>(`/api/auth/sessions/${id}/revoke`),
  revokeOthers: () => apiClient.post<void>('/api/auth/sessions/revoke-others'),
  loginAttempts: () => apiClient.get<LoginAttemptOut[]>('/api/auth/login-attempts'),
}

export const securityPolicyApi = {
  get: () => apiClient.get<SecurityPolicyOut>('/api/settings/security'),
  update: (payload: SecurityPolicyUpdate) =>
    apiClient.put<SecurityPolicyOut>('/api/settings/security', payload),
}
