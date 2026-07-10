/** Shared presentation helpers for the sites inventory (session 1.8). */

import type { Status } from '../components/types'
import type { Site, SiteHealth, SiteStatus } from '../api/sites'

/** Site lifecycle -> StatusDot color. Active = ok, vanished = warning. */
export function siteStatusDot(status: SiteStatus): Status {
  return status === 'active' ? 'ok' : 'warn'
}

export const SITE_STATUS_LABEL: Record<SiteStatus, string> = {
  active: 'Active',
  missing: 'Missing',
}

/**
 * Health -> StatusDot color. Driven by the external HTTP uptime checker
 * (session 2.7); `unknown` (no check yet) renders muted.
 */
export function healthDot(health: SiteHealth): Status {
  switch (health) {
    case 'ok':
      return 'ok'
    case 'warn':
      return 'warn'
    case 'err':
      return 'err'
    default:
      return 'muted'
  }
}

export const HEALTH_LABEL: Record<SiteHealth, string> = {
  unknown: 'Unknown',
  ok: 'Healthy',
  warn: 'Degraded',
  err: 'Down',
}

/** Scheduler tri-state label: on / off / not yet known (null from discovery). */
export function schedulerLabel(enabled: boolean | null): string {
  if (enabled === null) return 'Unknown'
  return enabled ? 'On' : 'Off'
}

/** Format an uptime % consistently: integer when ≥99.95, one decimal otherwise. */
export function pctLabel(pct: number | null | undefined): string {
  return pct == null ? '—' : `${pct.toFixed(pct >= 99.95 ? 0 : 1)}%`
}

/** Group a flat site list by bench_id, preserving first-seen order. */
export function groupByBench(sites: Site[]): Map<number, Site[]> {
  const groups = new Map<number, Site[]>()
  for (const site of sites) {
    const list = groups.get(site.bench_id) ?? []
    list.push(site)
    groups.set(site.bench_id, list)
  }
  return groups
}
