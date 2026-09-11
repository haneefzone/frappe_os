/**
 * Typed data layer for compliance report exports (session 4.4 — FDM 4.4).
 *
 * POST /api/compliance-reports/generate → binary file download.
 * The response carries the SHA-256 content hash in X-Content-Hash-SHA256 for
 * tamper-evidence verification. The download is triggered programmatically.
 */

const CSRF_COOKIE = 'fdm_csrf_token'
const CSRF_HEADER = 'X-CSRF-Token'

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

export type ReportType = 'access' | 'backup_evidence' | 'access_review'
export type ReportFormat = 'csv' | 'pdf'

export interface GenerateReportRequest {
  report_type: ReportType
  format: ReportFormat
  since?: string | null
  until?: string | null
}

export interface GenerateReportResult {
  /** The SHA-256 hex digest returned in X-Content-Hash-SHA256. */
  contentHash: string
  /** Filename from Content-Disposition. */
  filename: string
}

/**
 * POST /api/compliance-reports/generate and trigger a browser download.
 * Returns the content hash for tamper-evidence display.
 */
export async function generateComplianceReport(
  req: GenerateReportRequest,
): Promise<GenerateReportResult> {
  const response = await fetch('/api/compliance-reports/generate', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      [CSRF_HEADER]: readCookie(CSRF_COOKIE),
    },
    body: JSON.stringify(req),
  })

  if (!response.ok) {
    let message = `Export failed (HTTP ${response.status}).`
    try {
      const body = await response.json()
      message = body?.detail ?? body?.error?.message ?? message
    } catch {
      // ignore JSON parse failure
    }
    throw new Error(message)
  }

  const contentHash = response.headers.get('X-Content-Hash-SHA256') ?? ''

  // Derive filename from Content-Disposition or fall back to a sensible default.
  const cd = response.headers.get('Content-Disposition') ?? ''
  const nameMatch = cd.match(/filename="?([^";\s]+)"?/i)
  const filename =
    nameMatch?.[1] ?? `fdm-${req.report_type}-${new Date().toISOString().slice(0, 10)}.${req.format}`

  const blob = await response.blob()
  const href = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(href)

  return { contentHash, filename }
}
