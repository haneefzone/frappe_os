/**
 * Typed data layer for the Frappe app store (DOO-1206 / DOO-1194).
 * The catalog is the cached `frappe/marketplace` git checkout; compatibility
 * is computed server-side against a target bench's installed Frappe version.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export interface MarketplaceApp {
  name: string
  title: string
  description: string
  repo: string
  logo_url: string | null
  website: string | null
  documentation: string | null
  categories: string[]
  category: string | null
  stars: number
  /** null when no target bench was given. */
  is_installable: boolean | null
  /** Human-readable reason when is_installable is false. */
  reason: string | null
  /** Latest release version compatible with the target bench. */
  latest_compatible_version: string | null
}

export interface MarketplaceRelease {
  version: string
  branch: string
  commit: string
  frappe_core: string
  dependencies: Record<string, string>
  channel: string
  is_compatible: boolean | null
}

export interface PlanStep {
  app: string
  version: string
  branch: string
  commit: string
  repo: string
  channel: string
  /** e.g. "dependency of hrms" or "requested" */
  reason: string
}

export interface MarketplaceAppDetail extends MarketplaceApp {
  releases: MarketplaceRelease[]
  /** Ordered install plan (dependencies first). Null when plan_error is set. */
  plan: PlanStep[] | null
  /** Legible string when the plan cannot be resolved. */
  plan_error: string | null
}

export interface MarketplaceRefreshResult {
  refreshed: boolean
  served_stale: boolean
  app_count: number
}

export interface MarketplaceInstallPayload {
  app: string
  priority?: 'high' | 'default' | 'low'
}

export const marketplaceApi = {
  list: (opts?: { benchId?: number; category?: string; q?: string }) => {
    const p = new URLSearchParams()
    if (opts?.benchId != null) p.set('bench', String(opts.benchId))
    if (opts?.category) p.set('category', opts.category)
    if (opts?.q) p.set('q', opts.q)
    const qs = p.toString()
    return apiClient.get<MarketplaceApp[]>(`/api/marketplace/apps${qs ? `?${qs}` : ''}`)
  },

  detail: (name: string, benchId?: number) => {
    const qs = benchId != null ? `?bench=${benchId}` : ''
    return apiClient.get<MarketplaceAppDetail>(`/api/marketplace/apps/${encodeURIComponent(name)}${qs}`)
  },

  refresh: () => apiClient.post<MarketplaceRefreshResult>('/api/marketplace/refresh'),

  install: (siteId: number, payload: MarketplaceInstallPayload) =>
    apiClient.post<JobDetail>(`/api/sites/${siteId}/marketplace-apps`, payload),
}
