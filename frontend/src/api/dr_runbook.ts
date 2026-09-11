/**
 * Typed data layer for the DR runbook generator (session 4.3 — FDM 4.3).
 *
 * POST /api/dr-runbook/generate → binary file download (Markdown or PDF).
 * The response carries the SHA-256 content hash in X-Content-Hash-SHA256 for
 * tamper-evidence verification. The download is triggered programmatically.
 * RBAC (Admin/Developer) is enforced server-side — the button is only shown to
 * users who hold report:generate, but the endpoint is the source of truth.
 */

const CSRF_COOKIE = 'fdm_csrf_token'
const CSRF_HEADER = 'X-CSRF-Token'

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

export type RunbookFormat = 'md' | 'pdf'

export interface GenerateRunbookRequest {
  format: RunbookFormat
  /** Omit / null → whole-fleet runbook; otherwise scope to one server. */
  server_id?: number | null
}

export interface GenerateRunbookResult {
  /** The SHA-256 hex digest returned in X-Content-Hash-SHA256. */
  contentHash: string
  /** Filename from Content-Disposition. */
  filename: string
}

/**
 * POST /api/dr-runbook/generate and trigger a browser download.
 * Returns the content hash for tamper-evidence display.
 */
export async function generateDrRunbook(
  req: GenerateRunbookRequest,
): Promise<GenerateRunbookResult> {
  const response = await fetch('/api/dr-runbook/generate', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      [CSRF_HEADER]: readCookie(CSRF_COOKIE),
    },
    body: JSON.stringify(req),
  })

  if (!response.ok) {
    let message = `DR runbook generation failed (HTTP ${response.status}).`
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
  const scope = req.server_id ? `server-${req.server_id}` : 'fleet'
  const filename =
    nameMatch?.[1] ?? `dr-runbook-${scope}-${new Date().toISOString().slice(0, 10)}.${req.format}`

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
