/**
 * Typed data layer for the server registry (CLAUDE.md rule 9). CRUD goes
 * through apiClient; the connection test is a POST that streams Server-Sent
 * Events, which EventSource can't do (no POST, no cookies), so we read the
 * response body and parse the `data:` frames ourselves.
 */

import { apiClient } from './client'

export type EnvTag = 'prod' | 'staging' | 'dev'
export type AuthType = 'key' | 'password'
export type SudoMode = 'nopasswd' | 'none'
export type ServerStatus = 'unknown' | 'online' | 'offline' | 'error'

export interface CredentialInfo {
  username: string
  auth_type: AuthType
  sudo_mode: SudoMode
  has_private_key: boolean
  has_password: boolean
  host_key_pinned: boolean
}

export interface Server {
  id: number
  name: string
  hostname: string
  ssh_port: number
  os_version: string | null
  status: ServerStatus
  env_tag: EnvTag
  tags: string[]
  notes: string | null
  last_seen: string | null
  created_at: string
  updated_at: string
  credential: CredentialInfo | null
}

export interface ServerCreated extends Server {
  /** The generated public key, returned exactly once, for the user to install. */
  generated_public_key: string | null
}

export interface CredentialInput {
  username: string
  auth_type: AuthType
  sudo_mode: SudoMode
  generate?: boolean
  private_key?: string
  passphrase?: string
  password?: string
}

export interface ServerCreatePayload {
  name: string
  hostname: string
  ssh_port: number
  env_tag: EnvTag
  tags: string[]
  notes?: string | null
  credential: CredentialInput
}

/** One streamed frame from POST /api/servers/{id}/test. */
export interface CheckEvent {
  check: string // ssh | whoami | sudo | os | tool | done | error
  ok?: boolean
  value?: string | null
  name?: string // tool name, when check === 'tool'
  error?: string | null
  // present on the final `done` frame:
  ssh_ok?: boolean
  whoami?: string | null
  sudo_ok?: boolean
  lsb_release?: string | null
  tools?: Record<string, string | null>
}

const CSRF_COOKIE = 'fdm_csrf_token'
const CSRF_HEADER = 'X-CSRF-Token'

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

export const serversApi = {
  list: () => apiClient.get<Server[]>('/api/servers'),
  get: (id: number) => apiClient.get<Server>(`/api/servers/${id}`),
  create: (payload: ServerCreatePayload) =>
    apiClient.post<ServerCreated>('/api/servers', payload),
  update: (id: number, payload: Partial<ServerCreatePayload>) =>
    apiClient.patch<ServerCreated>(`/api/servers/${id}`, payload),
  remove: (id: number) => apiClient.delete<void>(`/api/servers/${id}`),
}

/**
 * Stream the connection test, invoking `onEvent` for each check as it lands.
 * Resolves when the stream closes; rejects if the request itself fails.
 */
export async function streamServerTest(
  id: number,
  onEvent: (event: CheckEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/servers/${id}/test`, {
    method: 'POST',
    headers: { [CSRF_HEADER]: readCookie(CSRF_COOKIE) },
    credentials: 'same-origin',
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`Connection test failed to start (HTTP ${response.status}).`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE frames are separated by a blank line.
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      const dataLine = frame.split('\n').find((line) => line.startsWith('data:'))
      if (dataLine) onEvent(JSON.parse(dataLine.slice(5).trim()) as CheckEvent)
    }
  }
}
