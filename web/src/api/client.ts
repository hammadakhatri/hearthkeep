/** The one way the web app talks to the Hearthkeep server. */

/** Fired on `window` whenever the server says the session is gone (any 401). */
export const UNAUTHENTICATED_EVENT = 'hearthkeep:unauthenticated'

export class ApiError extends Error {
  readonly status: number
  readonly reason: string
  readonly body: unknown

  /** `status` 0 with reason "network" means the server could not be reached at all. */
  constructor(status: number, reason: string, body: unknown = null) {
    super(`${status} ${reason}`)
    this.name = 'ApiError'
    this.status = status
    this.reason = reason
    this.body = body
  }
}

export function isApiError(error: unknown, reason?: string): error is ApiError {
  return error instanceof ApiError && (reason === undefined || error.reason === reason)
}

/**
 * Fetch a JSON endpoint. Adds the `X-Hearthkeep: 1` header the server requires on every
 * state-changing request, sends the HttpOnly session cookie (same origin only), and throws
 * `ApiError` for any non-2xx answer or network failure.
 */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('X-Hearthkeep', '1')
  if (typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  let response: Response
  try {
    response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'network')
  }

  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }

  if (!response.ok) {
    const reason =
      data && typeof data === 'object' && typeof (data as { reason?: unknown }).reason === 'string'
        ? (data as { reason: string }).reason
        : 'http_error'
    if (response.status === 401) window.dispatchEvent(new Event(UNAUTHENTICATED_EVENT))
    throw new ApiError(response.status, reason, data)
  }
  return data as T
}

/** `init` for a JSON POST. */
export function post(body: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(body) }
}
