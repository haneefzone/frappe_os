/**
 * Typed data layer for domains & SSL (FDM 2.4, CLAUDE.md rule 9).
 * List/add/remove are config-row operations (no job); test-dns, render-vhost,
 * and issue-cert each enqueue a remote job and return JobDetail.
 */

import { apiClient } from './client'
import type { JobDetail } from './jobs'

export type CertStatus = 'none' | 'issued' | 'error'

export interface DomainOut {
  id: number
  site_id: number
  domain: string
  is_primary: boolean
  ssl_enabled: boolean
  cert_status: CertStatus | string
  cert_expires_at: string | null
  /** Backend-computed `(cert_expires_at - now).days`; null when no cert. */
  days_left: number | null
  dns_ok: boolean | null
  last_checked: string | null
  last_error: string | null
  created_at: string
  updated_at: string
}

export interface CreateDomainPayload {
  domain: string
  is_primary?: boolean
}

export interface DomainActionPayload {
  priority?: 'high' | 'default' | 'low'
}

export interface IssueCertPayload {
  email: string
  priority?: 'high' | 'default' | 'low'
}

/** Hostname pattern from the backend (DOMAIN_NAME registry constant). */
export const HOSTNAME_RE =
  /^[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))+$/

export const domainsApi = {
  list: (siteId: number) => apiClient.get<DomainOut[]>(`/api/sites/${siteId}/domains`),

  add: (siteId: number, payload: CreateDomainPayload) =>
    apiClient.post<DomainOut>(`/api/sites/${siteId}/domains`, payload),

  remove: (siteId: number, domainId: number) =>
    apiClient.delete<{ ok: boolean; domain: string }>(`/api/sites/${siteId}/domains/${domainId}`),

  testDns: (siteId: number, domainId: number, payload?: DomainActionPayload) =>
    apiClient.post<JobDetail>(
      `/api/sites/${siteId}/domains/${domainId}/test-dns`,
      payload ?? {},
    ),

  renderVhost: (siteId: number, domainId: number, payload?: DomainActionPayload) =>
    apiClient.post<JobDetail>(
      `/api/sites/${siteId}/domains/${domainId}/render-vhost`,
      payload ?? {},
    ),

  issueCert: (siteId: number, domainId: number, payload: IssueCertPayload) =>
    apiClient.post<JobDetail>(
      `/api/sites/${siteId}/domains/${domainId}/issue-cert`,
      payload,
    ),
}
