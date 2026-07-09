/** Shared presentation helpers for the bench inventory. */

import type { Status } from '../components/types'
import type { Bench, BenchStatus } from '../api/benches'

/** Bench lifecycle -> StatusDot color. Active = ok, vanished = warning. */
export function benchStatusDot(status: BenchStatus): Status {
  return status === 'active' ? 'ok' : 'warn'
}

export const BENCH_STATUS_LABEL: Record<BenchStatus, string> = {
  active: 'Active',
  missing: 'Missing',
}

/**
 * The version chip label: the major of the parsed frappe version as `vNN`
 * (v14/v15/v16). The chip itself is a neutral grey (design spec: colored by
 * status tokens only — a grey chip with text), so we return just the text.
 */
export function versionChip(frappeVersion: string | null): string {
  if (!frappeVersion) return 'unknown'
  const major = frappeVersion.split('.')[0]
  return /^\d+$/.test(major) ? `v${major}` : frappeVersion
}

/** The six ports as label/value rows for the port-map popover. */
export function portRows(bench: Bench): { label: string; value: number | null }[] {
  return [
    { label: 'Web', value: bench.ports.webserver_port },
    { label: 'Socket.IO', value: bench.ports.socketio_port },
    { label: 'Redis cache', value: bench.ports.redis_cache_port },
    { label: 'Redis queue', value: bench.ports.redis_queue_port },
    { label: 'Redis socketio', value: bench.ports.redis_socketio_port },
    { label: 'File watcher', value: bench.ports.file_watcher_port },
  ]
}

/** Group a flat bench list by server_id, preserving first-seen order. */
export function groupByServer(benches: Bench[]): Map<number, Bench[]> {
  const groups = new Map<number, Bench[]>()
  for (const bench of benches) {
    const list = groups.get(bench.server_id) ?? []
    list.push(bench)
    groups.set(bench.server_id, list)
  }
  return groups
}
