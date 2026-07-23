/**
 * Typed data layer for scoped AI CLI agents (session 5.1, CLAUDE.md rule 9).
 * Everything rides through apiClient. A session starts with a pre-change
 * snapshot job; when it is 'ready' we mint a terminal ticket and open a WS to
 * the SAME endpoint the interactive terminal uses. Ending a session captures a
 * diff the operator applies or rolls back — each of those is a tracked job.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'
import { terminalApi } from './terminal'

export type AgentKind = 'claude-code' | 'custom'

export type SessionStatus =
  | 'starting'
  | 'ready'
  | 'reviewing'
  | 'applied'
  | 'rolledback'
  | 'error'

export type Disposition = 'applied' | 'rolledback'

export interface AgentConfig {
  id: number
  name: string
  kind: AgentKind
  command_template: string
  working_dir: string
  read_only: boolean
  pre_change_backup: boolean
  allowed_server_ids: number[]
  created_at: string
  updated_at: string
}

export interface CreateAgentPayload {
  name: string
  kind: AgentKind
  command_template: string
  working_dir: string
  read_only: boolean
  pre_change_backup: boolean
  allowed_server_ids: number[]
}

export type UpdateAgentPayload = Partial<CreateAgentPayload>

export interface Session {
  id: number
  agent_id: number | null
  agent_name: string | null
  server_id: number
  server_name: string | null
  user_id: number | null
  ssh_username: string
  working_dir: string
  read_only: boolean
  pre_change_backup: boolean
  status: SessionStatus
  base_commit: string | null
  disposition: Disposition | null
  diff_text: string | null
  snapshot_job_id: number | null
  diff_job_id: number | null
  resolve_job_id: number | null
  close_reason: string | null
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
}

/** Ticket payload for opening the scoped agent terminal (mirrors terminal.ts). */
export interface SessionTicket {
  session_id: number
  ticket: string
  server_name: string
  ssh_username: string
  working_dir: string
  read_only: boolean
  ticket_ttl_seconds: number
}

export const aiAgentsApi = {
  // --- agent configs -------------------------------------------------------
  list: () => apiClient.get<AgentConfig[]>('/api/ai-agents'),
  get: (id: number) => apiClient.get<AgentConfig>(`/api/ai-agents/${id}`),
  create: (payload: CreateAgentPayload) =>
    apiClient.post<AgentConfig>('/api/ai-agents', payload),
  update: (id: number, payload: UpdateAgentPayload) =>
    apiClient.patch<AgentConfig>(`/api/ai-agents/${id}`, payload),
  remove: (id: number) => apiClient.delete<void>(`/api/ai-agents/${id}`),

  // --- sessions ------------------------------------------------------------
  startSession: (agentId: number, serverId: number) =>
    apiClient.post<Session>(`/api/ai-agents/${agentId}/sessions`, { server_id: serverId }),
  listSessions: () => apiClient.get<Session[]>('/api/ai-agents/sessions'),
  getSession: (sid: number) => apiClient.get<Session>(`/api/ai-agents/sessions/${sid}`),
  /** Mint a WS ticket; only succeeds when the session is 'ready' (409 otherwise). */
  terminalTicket: (sid: number) =>
    apiClient.post<SessionTicket>(`/api/ai-agents/sessions/${sid}/terminal`, {}),
  /** Capture the diff and move the session into review. Returns the diff job. */
  endSession: (sid: number) =>
    apiClient.post<JobDetail>(`/api/ai-agents/sessions/${sid}/end`, {}),
  /** Apply the captured changes. Forbidden (403) for read-only sessions. */
  apply: (sid: number) =>
    apiClient.post<JobDetail>(`/api/ai-agents/sessions/${sid}/apply`, {}),
  /** Discard changes and restore the pre-change snapshot. */
  rollback: (sid: number) =>
    apiClient.post<JobDetail>(`/api/ai-agents/sessions/${sid}/rollback`, {}),
}

/**
 * Build the WS URL for an agent terminal ticket. The endpoint is identical to
 * the interactive terminal's, so reuse its helper verbatim.
 */
export function wsUrl(ticket: string): string {
  return terminalApi.wsUrl(ticket)
}
