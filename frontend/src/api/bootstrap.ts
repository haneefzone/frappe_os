/**
 * Typed data layer for the first-run installation wizard (session 6.4).
 * These routes are public and unauthenticated — no CSRF header needed on reads,
 * but the apiClient adds it to POST calls automatically via the double-submit cookie.
 * We use rawRequest (no auth retry) because there are no auth cookies yet.
 */

export interface BootstrapStatus {
  needs_setup: boolean
}

export interface PreflightCheck {
  name: string
  ok: boolean
  detail: string
  hint?: string | null
}

export interface PreflightResult {
  checks: PreflightCheck[]
  all_ok: boolean
}

export interface AdminIn {
  email: string
  full_name: string
  password: string
}

export interface BrandingIn {
  product_name?: string
  default_tz?: string
  accent_hex?: string | null
}

export interface NotificationsIn {
  smtp_host?: string | null
  smtp_port?: number
  smtp_from?: string | null
  smtp_username?: string | null
  smtp_password?: string | null
}

export interface CompleteIn {
  admin: AdminIn
  branding?: BrandingIn
  notifications?: NotificationsIn | null
}

export interface CompleteOut {
  setup_complete: boolean
  user_id: number
  email: string
}

async function _post<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const response = await fetch(path, {
    method: 'POST',
    headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText)
    throw { status: response.status, detail: text }
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const bootstrapApi = {
  status: () => fetch('/api/bootstrap/status').then((r) => r.json() as Promise<BootstrapStatus>),

  preflight: () => _post<PreflightResult>('/api/bootstrap/preflight'),

  complete: (payload: CompleteIn) => _post<CompleteOut>('/api/bootstrap/complete', payload),
}
