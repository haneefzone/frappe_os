/** Shared component-library types. Status colors are the ONLY colors in the UI. */

export type Status = 'ok' | 'warn' | 'err' | 'running' | 'muted'

export type Environment = 'prod' | 'staging' | 'dev'

/** Hex values must match the tokens in tailwind.config.js / index.css. */
export const STATUS_COLOR: Record<Status, string> = {
  ok: '#22C55E',
  warn: '#F59E0B',
  err: '#EF4444',
  running: '#3B82F6',
  muted: 'var(--text-muted)',
}

export interface DataTableColumn<Row> {
  /** Row property to read (also the slot suffix: #cell-<key>). */
  key: string
  label: string
  sortable?: boolean
  align?: 'left' | 'center' | 'right'
  /** CSS width, e.g. '120px' or '12rem'. */
  width?: string
  /** Custom display string; sorting and filtering use it too when present. */
  format?: (row: Row) => string
  /** Start hidden; user can enable it from the column chooser. */
  hidden?: boolean
}

export type JobStepStatus = 'pending' | 'running' | 'done' | 'failed'

export interface JobStep {
  label: string
  status: JobStepStatus
  /** Shown next to finished steps, e.g. '42s'. */
  duration?: string
  /** Epoch ms; running steps show a live elapsed counter from this. */
  startedAt?: number
  /** Failure detail shown in the expandable error area. */
  error?: string
}

export interface WizardStep {
  /** Slot suffix: #step-<key>. */
  key: string
  label: string
  description?: string
}
