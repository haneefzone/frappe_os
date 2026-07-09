/**
 * Typed data layer for the job engine (CLAUDE.md rule 9). CRUD rides through
 * apiClient; the live log tail is a GET that streams Server-Sent Events, which
 * we read off the response body ourselves (EventSource can't send our CSRF-less
 * cookies with the right options and can't resume with a custom ?after_seq).
 */

import { apiClient } from './client'

export type JobStatus = 'pending' | 'running' | 'success' | 'failure' | 'cancelled'
export type StepStatus = 'pending' | 'running' | 'success' | 'failure' | 'skipped'
export type Priority = 'high' | 'default' | 'low'
export type TargetType = 'server' | 'bench' | 'site'

export interface JobStepOut {
  id: number
  name: string
  /** 1-based auto-retry attempt this step belongs to (present once DOO-96 lands). */
  attempt?: number
  order: number
  status: StepStatus
  started_at: string | null
  ended_at: string | null
  error_traceback: string | null
}

export interface Job {
  id: number
  server_id: number
  target_type: string
  target_id: string | null
  action_name: string
  priority: string
  status: JobStatus
  rq_job_id: string | null
  retry_count: number
  exit_code: number | null
  lock_key: string | null
  params_sanitized: Record<string, unknown>
  created_by: number | null
  started_at: string | null
  ended_at: string | null
  created_at: string
  updated_at: string
}

export interface JobDetail extends Job {
  steps: JobStepOut[]
}

export interface JobCreatePayload {
  action_name: string
  server_id: number
  target_type?: TargetType
  target_id?: string | null
  priority?: Priority
  params?: Record<string, unknown>
}

export interface JobListParams {
  status?: JobStatus
  server?: number
  mine?: boolean
  limit?: number
}

export interface JobCommand {
  command: string
  argv: string[]
}

function queryString(params: JobListParams): string {
  const q = new URLSearchParams()
  if (params.status) q.set('status', params.status)
  if (params.server != null) q.set('server', String(params.server))
  if (params.mine) q.set('mine', 'true')
  if (params.limit != null) q.set('limit', String(params.limit))
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const jobsApi = {
  list: (params: JobListParams = {}) =>
    apiClient.get<Job[]>(`/api/jobs${queryString(params)}`),
  get: (id: number) => apiClient.get<JobDetail>(`/api/jobs/${id}`),
  create: (payload: JobCreatePayload) => apiClient.post<JobDetail>('/api/jobs', payload),
  cancel: (id: number) => apiClient.post<JobDetail>(`/api/jobs/${id}/cancel`),
  retry: (id: number) => apiClient.post<JobDetail>(`/api/jobs/${id}/retry`),
  command: (id: number) => apiClient.get<JobCommand>(`/api/jobs/${id}/command`),
}

/** One streamed `event: log` frame from the SSE endpoint. */
export interface LogFrame {
  seq: number
  stream: string
  content: string
}

export interface StreamHandlers {
  onLog: (frame: LogFrame) => void
  /** Server signalled the job is terminal and the stream is closing. */
  onEnd?: (info: { status: string; last_seq: number }) => void
  /** Stream opened (first `: connected` comment). */
  onOpen?: () => void
}

/**
 * Tail a job's logs over SSE, resuming after `afterSeq`. Resolves when the
 * server closes the stream (terminal `end` event) or the caller aborts via
 * `signal`; rejects only if the request itself fails to start. The caller owns
 * reconnection: on an unexpected close of a still-running job, call again with
 * the highest seq seen so far — replay guarantees no gaps and no duplicates.
 */
export async function streamJobLogs(
  id: number,
  afterSeq: number,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/jobs/${id}/logs/stream?after_seq=${afterSeq}`, {
    method: 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`Log stream failed to start (HTTP ${response.status}).`)
  }
  handlers.onOpen?.()

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>
    try {
      chunk = await reader.read()
    } catch {
      // Aborted (component unmounted / navigated away) — a clean stop.
      return
    }
    if (chunk.done) break
    buffer += decoder.decode(chunk.value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      // A frame is a set of `field: value` lines; comments (`: ...`) are skipped.
      let event = 'message'
      let data = ''
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      if (event === 'log') handlers.onLog(JSON.parse(data) as LogFrame)
      else if (event === 'end') handlers.onEnd?.(JSON.parse(data))
    }
  }
}
