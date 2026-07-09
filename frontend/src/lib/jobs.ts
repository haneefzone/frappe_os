/** Shared presentation helpers for the job engine UI. */

import type { JobStep, Status } from '../components/types'
import type { JobStatus, JobStepOut, StepStatus } from '../api/jobs'

/** Map a job's lifecycle status to a StatusDot/Badge color. */
export function jobStatusDot(status: JobStatus): Status {
  switch (status) {
    case 'success':
      return 'ok'
    case 'failure':
      return 'err'
    case 'cancelled':
      return 'warn'
    case 'running':
      return 'running'
    default:
      return 'muted' // pending
  }
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  pending: 'Pending',
  running: 'Running',
  success: 'Success',
  failure: 'Failed',
  cancelled: 'Cancelled',
}

export const TERMINAL_STATUSES: JobStatus[] = ['success', 'failure', 'cancelled']

export function isTerminal(status: JobStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}

/** A compact human duration between two ISO timestamps (or to now if open). */
export function durationBetween(startIso: string | null, endIso: string | null): string {
  if (!startIso) return '—'
  const start = new Date(startIso).getTime()
  const end = endIso ? new Date(endIso).getTime() : Date.now()
  return formatDuration(Math.max(0, end - start))
}

export function formatDuration(ms: number): string {
  const total = Math.floor(ms / 1000)
  if (total < 60) return `${total}s`
  const m = Math.floor(total / 60)
  const s = total % 60
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

/** Backend step status → the JobTimeline component's four-state vocabulary. */
function timelineStatus(status: StepStatus): JobStep['status'] {
  switch (status) {
    case 'running':
      return 'running'
    case 'success':
      return 'done'
    case 'failure':
      return 'failed'
    default:
      return 'pending' // pending | skipped
  }
}

/**
 * Adapt persisted `CommandStep` rows to the `JobStep` shape JobTimeline renders.
 * Rows arrive already ordered by the API (by attempt then order once DOO-96
 * lands, else by order); we render them in that order rather than re-sorting.
 */
export function toTimelineSteps(steps: JobStepOut[]): JobStep[] {
  return steps.map((step) => ({
    label: step.name,
    status: timelineStatus(step.status),
    duration:
      step.started_at && step.ended_at
        ? durationBetween(step.started_at, step.ended_at)
        : undefined,
    startedAt: step.started_at ? new Date(step.started_at).getTime() : undefined,
    error: step.error_traceback ?? undefined,
  }))
}

/** Index of the first failed step, or -1 — the detail page auto-expands it. */
export function firstFailedIndex(steps: JobStepOut[]): number {
  return steps.findIndex((s) => s.status === 'failure')
}
