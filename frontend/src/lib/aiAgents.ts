/** Shared presentation helpers for scoped AI agent sessions. */

import type { Status } from '../components/types'
import type { AgentKind, SessionStatus } from '../api/aiAgents'

/** Map a session's lifecycle status to a StatusDot/Badge color. */
export function sessionStatusDot(status: SessionStatus): Status {
  switch (status) {
    case 'ready':
    case 'applied':
      return 'ok'
    case 'error':
      return 'err'
    case 'rolledback':
      return 'warn'
    case 'starting':
    case 'reviewing':
      return 'running'
    default:
      return 'muted'
  }
}

export const SESSION_STATUS_LABEL: Record<SessionStatus, string> = {
  starting: 'Preparing',
  ready: 'Ready',
  reviewing: 'In review',
  applied: 'Applied',
  rolledback: 'Rolled back',
  error: 'Error',
}

export const AGENT_KIND_LABEL: Record<AgentKind, string> = {
  'claude-code': 'Claude Code',
  custom: 'Custom CLI',
}

/** Sessions whose diff can be reviewed / acted on from the review screen. */
export function isReviewable(status: SessionStatus): boolean {
  return status === 'reviewing' || status === 'applied' || status === 'rolledback'
}
