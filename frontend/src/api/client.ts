/**
 * The typed data layer (CLAUDE.md rule 9): every API call goes through here,
 * never through frappe-ui resources. Sessions ride in httpOnly cookies; this
 * client adds the CSRF double-submit header on mutations, transparently
 * retries one refresh on 401, and hands terminal 401s to the auth store.
 */

export interface ApiErrorBody {
  error: { code: string; message: string }
}

export class ApiError extends Error {
  status: number
  code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

export interface UserInfo {
  id: number
  email: string
  full_name: string
  role: string
  permissions: string[]
  last_login: string | null
}

const CSRF_COOKIE = 'fdm_csrf_token'
const CSRF_HEADER = 'X-CSRF-Token'

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

/** Registered by the auth store; called when a session is gone for good. */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as ApiErrorBody
    return new ApiError(response.status, body.error.code, body.error.message)
  } catch {
    return new ApiError(response.status, 'http_error', response.statusText)
  }
}

async function rawRequest(method: string, path: string, body?: unknown): Promise<Response> {
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers[CSRF_HEADER] = readCookie(CSRF_COOKIE)
  return fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response = await rawRequest(method, path, body)

  // Access tokens live 15 minutes; one silent refresh-and-retry covers expiry.
  if (response.status === 401 && !path.startsWith('/api/auth/')) {
    const refreshed = await rawRequest('POST', '/api/auth/refresh')
    if (refreshed.ok) {
      response = await rawRequest(method, path, body)
    } else {
      onUnauthorized?.()
      throw await parseError(response)
    }
  }

  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/auth/')) onUnauthorized?.()
    throw await parseError(response)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const apiClient = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  // A body is optional but supported — a destructive DELETE carries the
  // type-the-target-name confirmation (CLAUDE.md rule 5).
  delete: <T>(path: string, body?: unknown) => request<T>('DELETE', path, body),
}

export const authApi = {
  login: (email: string, password: string) =>
    apiClient.post<UserInfo>('/api/auth/login', { email, password }),
  me: () => apiClient.get<UserInfo>('/api/auth/me'),
  logout: () => apiClient.post<void>('/api/auth/logout'),
}
