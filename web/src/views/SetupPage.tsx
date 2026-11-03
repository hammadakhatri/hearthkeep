import { useEffect, useRef, useState } from 'react'
import { isApiError } from '../api/client'
import { page, waitForSetupDone } from '../api/server'
import { readSetupSecret, setupEnrollmentCode, setupStatus } from '../api/setup'
import { enroll, passkeyErrorMessage } from '../api/webauthn'
import { PasskeyIcon } from '../shell/icons'
import { guessDeviceName, Note } from './SignIn'

type Stage = 'no-secret' | 'checking' | 'ended' | 'away' | 'ready' | 'finishing' | 'slow'

const ENDED = 'This setup link has ended. Double-click Setup.cmd in the Hearthkeep folder to start again.'
const BUSY = 'Busy for a moment, trying again…'

/** How long to wait after a 429 before asking again. Other programs on this PC share the limit. */
const BUSY_RETRY_MS = 5000

type Props = {
  /** Setup is over (or was never on): go to the normal app. */
  onLeave: () => void
  retryMs?: number
}

/** `/setup`: the first passkey, made with the secret from the setup link. */
export function SetupPage({ onLeave, retryMs = BUSY_RETRY_MS }: Props) {
  // Read once: reading clears it from the address bar, so a second read would find nothing.
  const firstRead = useRef<{ value: string | null } | null>(null)
  if (firstRead.current === null) firstRead.current = { value: readSetupSecret() }
  // Held in memory only, never rendered. A setup link opened again in this tab replaces it.
  const [secret, setSecret] = useState(firstRead.current.value)

  const [stage, setStage] = useState<Stage>(secret ? 'checking' : 'no-secret')
  const [busyRetry, setBusyRetry] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const alive = useRef(true)
  const leave = useRef(onLeave)
  leave.current = onLeave

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // A setup link opened in this tab again only changes the fragment: take it, clear it, check it.
  useEffect(() => {
    const onHash = () => {
      const next = readSetupSecret()
      if (!next) return
      setSecret(next)
      setError(null)
      setAttempt((n) => n + 1)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  /** `call`, asked again after a pause while the server says it is busy (429). */
  async function patiently<T>(call: () => Promise<T>, live: () => boolean): Promise<T> {
    for (;;) {
      try {
        const result = await call()
        if (live()) setBusyRetry(false)
        return result
      } catch (e) {
        if (!isApiError(e) || e.status !== 429 || !live()) throw e
        setBusyRetry(true)
        await new Promise((resolve) => window.setTimeout(resolve, retryMs))
        if (!live()) throw e
      }
    }
  }

  useEffect(() => {
    if (!secret) return
    let live = true
    setStage('checking')
    patiently(() => setupStatus(secret), () => live)
      .then(() => live && setStage('ready'))
      .catch((e: unknown) => {
        if (!live) return
        if (isApiError(e) && e.status === 404) leave.current()
        else if (isApiError(e) && e.status === 403) setStage('ended')
        else setStage('away')
      })
    return () => {
      live = false
    }
    // `patiently` only uses state setters and `retryMs`; a new attempt is the Try again button.
  }, [secret, attempt])

  async function createPasskey() {
    if (!secret || creating) return
    setCreating(true)
    setError(null)
    let code: string
    try {
      code = (await patiently(() => setupEnrollmentCode(secret), () => alive.current)).code
    } catch (e) {
      if (!alive.current) return
      setCreating(false)
      if (isApiError(e) && e.status === 404) leave.current()
      else if (isApiError(e) && e.status === 403) setStage('ended')
      else setError(passkeyErrorMessage(e, 'Setup'))
      return
    }
    try {
      await enroll(code, guessDeviceName())
    } catch (e) {
      if (!alive.current) return
      setCreating(false)
      setError(passkeyErrorMessage(e, 'Setup'))
      return
    }
    if (!alive.current) return
    setStage('finishing')
    if (await waitForSetupDone()) {
      page.replace('/')
      return
    }
    if (alive.current) setStage('slow')
  }

  return (
    <main className="signin">
      <div className="signin-card">
        <span className={`mark signin-mark${stage === 'away' ? ' away' : ''}`} aria-hidden="true" />
        <h1 className="signin-title compact">Set up Hearthkeep</h1>
        {stage === 'no-secret' && (
          <p className="signin-lede">Open setup from this PC: double-click Setup.cmd in the Hearthkeep folder.</p>
        )}
        {stage === 'ended' && <p className="signin-lede">{ENDED}</p>}
        {stage === 'away' && (
          <>
            <p className="signin-lede">Can't reach Hearthkeep. Check that the setup window is still open on this PC.</p>
            <div className="signin-actions">
              <button type="button" className="btn primary lg block" onClick={() => setAttempt((n) => n + 1)}>
                Try again
              </button>
            </div>
          </>
        )}
        {stage === 'checking' && (
          <div className="signin-actions" aria-busy="true">
            <p className="signin-lede" role="status" aria-live="polite">
              {busyRetry ? BUSY : 'Checking the setup link…'}
            </p>
          </div>
        )}
        {stage === 'ready' && (
          <>
            <p className="signin-lede">One last step: create the passkey you'll use to open Hearthkeep on this PC.</p>
            <div className="signin-actions">
              <p className="hint" role="status" aria-live="polite">
                {busyRetry ? BUSY : ''}
              </p>
              {error && <Note kind="error">{error}</Note>}
              <button
                type="button"
                className="btn primary lg block"
                onClick={() => void createPasskey()}
                disabled={creating}
                aria-busy={creating}
              >
                <PasskeyIcon />
                {creating ? 'Waiting for your passkey…' : 'Create passkey'}
              </button>
            </div>
          </>
        )}
        {(stage === 'finishing' || stage === 'slow') && (
          <div className="signin-actions">
            <p className="signin-lede" role="status" aria-live="polite">
              {stage === 'finishing'
                ? 'Finishing setup…'
                : 'Hearthkeep is taking a while to start. Open it from the Start menu in a minute.'}
            </p>
          </div>
        )}
      </div>
    </main>
  )
}
