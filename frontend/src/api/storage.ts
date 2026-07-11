/**
 * Typed data layer for S3-compatible storage targets (session 2.2, CLAUDE.md
 * rule 9). A target is one bucket the platform pushes backup artifacts to for
 * offsite retention. The access/secret keys are write-only: sent on create/
 * update, Fernet-encrypted server-side, and NEVER returned — the read model
 * exposes only a `keys_set` boolean (rule 6).
 */

import { apiClient } from './client'

export type StorageProvider = 'aws' | 'minio' | 'backblaze' | 'wasabi' | 'other'

export interface StorageTarget {
  id: number
  name: string
  provider: string
  endpoint_url: string | null
  region: string | null
  bucket: string
  path_prefix: string | null
  use_ssl: boolean
  enabled: boolean
  /** Whether both credentials are set (the values themselves are never sent). */
  keys_set: boolean
  created_at: string
  updated_at: string
}

export interface StorageTargetCreate {
  name: string
  provider: StorageProvider
  endpoint_url?: string | null
  region?: string | null
  bucket: string
  path_prefix?: string | null
  access_key: string
  secret_key: string
  use_ssl: boolean
  enabled: boolean
}

/**
 * Patch a target. Keys are optional: a non-empty value re-encrypts, an empty
 * string clears them, and omitting them leaves the stored keys untouched.
 */
export interface StorageTargetUpdate {
  name?: string
  provider?: StorageProvider
  endpoint_url?: string | null
  region?: string | null
  bucket?: string
  path_prefix?: string | null
  access_key?: string
  secret_key?: string
  use_ssl?: boolean
  enabled?: boolean
}

export interface TestConnectionResult {
  reachable: boolean
  writable: boolean
  latency_ms: number | null
  error: string | null
}

export const storageApi = {
  list: () => apiClient.get<StorageTarget[]>('/api/storage-targets'),
  get: (id: number) => apiClient.get<StorageTarget>(`/api/storage-targets/${id}`),
  create: (payload: StorageTargetCreate) =>
    apiClient.post<StorageTarget>('/api/storage-targets', payload),
  update: (id: number, payload: StorageTargetUpdate) =>
    apiClient.patch<StorageTarget>(`/api/storage-targets/${id}`, payload),
  remove: (id: number) => apiClient.delete<void>(`/api/storage-targets/${id}`),
  testConnection: (id: number) =>
    apiClient.post<TestConnectionResult>(`/api/storage-targets/${id}/test-connection`),
}

export const STORAGE_PROVIDER_LABEL: Record<string, string> = {
  aws: 'AWS S3',
  minio: 'MinIO',
  backblaze: 'Backblaze B2',
  wasabi: 'Wasabi',
  other: 'S3-compatible',
}
