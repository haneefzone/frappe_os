/** Shared presentation helpers for the server registry. */

import type { Status } from '../components/types'
import type { ServerStatus } from '../api/servers'

/** Map a server's lifecycle status to a StatusDot color. */
export function statusDot(status: ServerStatus): Status {
  switch (status) {
    case 'online':
      return 'ok'
    case 'error':
      return 'err'
    case 'offline':
      return 'warn'
    default:
      return 'muted'
  }
}

export const STATUS_LABEL: Record<ServerStatus, string> = {
  online: 'Online',
  offline: 'Offline',
  error: 'Error',
  unknown: 'Not tested',
}

/** Relative "3 min ago" style time; absolute string suitable for a title/hover. */
export function relativeTime(iso: string | null): string {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 45) return 'just now'
  const units: [number, string][] = [
    [60, 'sec'],
    [60, 'min'],
    [24, 'hr'],
    [7, 'day'],
    [4.348, 'wk'],
    [12, 'mo'],
    [Number.POSITIVE_INFINITY, 'yr'],
  ]
  let value = seconds
  let unit = 'sec'
  for (const [size, name] of units) {
    if (value < size) {
      unit = name
      break
    }
    value = value / size
    unit = name
  }
  const rounded = Math.round(value)
  return `${rounded} ${unit}${rounded === 1 ? '' : 's'} ago`
}

export function absoluteTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : ''
}
