import { apiClient } from './client'

export interface SessionCreated {
  session_id: number
  ticket: string
  server_name: string
  ssh_username: string
  ticket_ttl_seconds: number
}

export interface TerminalSessionOut {
  id: number
  server_id: number
  server_name: string | null
  user_id: number | null
  user_email: string | null
  ssh_username: string
  status: string
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  close_reason: string | null
}

export const terminalApi = {
  createSession(serverId: number): Promise<SessionCreated> {
    return apiClient.post('/api/terminal/sessions', { server_id: serverId })
  },

  listSessions(): Promise<TerminalSessionOut[]> {
    return apiClient.get('/api/terminal/sessions')
  },

  /** Build the WS URL for a given ticket. Adapts ws:// or wss:// to match
   *  the page protocol so the connection works in both dev and prod. */
  wsUrl(ticket: string): string {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    return `${proto}//${host}/api/terminal/ws?ticket=${encodeURIComponent(ticket)}`
  },
}
