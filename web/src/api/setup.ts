/** First-run setup: the secret from the setup link, and the two setup endpoints.
 *
 * The secret arrives in the address bar's fragment (`/setup#<secret>`), which never reaches the
 * server. It is read once, taken out of the address bar and the history, and only ever sent in a
 * POST body: never a query string, storage or a log. */

import { api, post } from './client'

const SECRET = /^#[A-Za-z0-9_-]{20,128}$/

/** The setup secret from the fragment, or null. Any fragment is cleared from the address bar. */
export function readSetupSecret(): string | null {
  const hash = window.location.hash
  if (!hash) return null
  window.history.replaceState(null, '', '/setup')
  return SECRET.test(hash) ? hash.slice(1) : null
}

/** Whether setup is still waiting for the passkey. 404 once setup is over, 403 for a wrong secret. */
export function setupStatus(secret: string): Promise<{ state: 'needs_passkey' }> {
  return api('/api/setup/status', post({ secret }))
}

/** The code for the first passkey. Kept by the caller in a local variable only. */
export function setupEnrollmentCode(secret: string): Promise<{ code: string; expires_in_minutes: number }> {
  return api('/api/setup/enrollment-code', post({ secret }))
}
