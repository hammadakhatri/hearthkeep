import { useCallback, useEffect, useRef, useState } from 'react'
import { api, isApiError, UNAUTHENTICATED_EVENT } from './api/client'
import { AppShell } from './shell/AppShell'
import { SignIn } from './views/SignIn'

type Auth = 'checking' | 'unreachable' | 'signed-out' | 'signed-in'

/** No answer at all, or the server failing: not a reason to ask the owner to sign in again. */
function serverAway(error: unknown): boolean {
  return !isApiError(error) || error.status === 0 || error.status >= 500
}

export default function App() {
  const [auth, setAuth] = useState<Auth>('checking')
  const alive = useRef(true)

  const checkSession = useCallback(() => {
    setAuth('checking')
    api('/api/auth/session')
      .then(() => alive.current && setAuth('signed-in'))
      .catch((error: unknown) => alive.current && setAuth(serverAway(error) ? 'unreachable' : 'signed-out'))
  }, [])

  useEffect(() => {
    alive.current = true
    checkSession()
    const expired = () => setAuth('signed-out')
    window.addEventListener(UNAUTHENTICATED_EVENT, expired)
    return () => {
      alive.current = false
      window.removeEventListener(UNAUTHENTICATED_EVENT, expired)
    }
  }, [checkSession])

  async function signOut() {
    try {
      await api('/api/auth/logout', { method: 'POST' })
    } catch {
      // Signed out locally either way; the session is already gone or the server is away.
    }
    setAuth('signed-out')
  }

  if (auth === 'checking') {
    return (
      <div className="splash" aria-busy="true" aria-label="Opening Hearthkeep">
        <span className="mark splash-mark" aria-hidden="true" />
      </div>
    )
  }
  if (auth === 'unreachable') return <Unreachable onRetry={checkSession} />
  if (auth === 'signed-out') return <SignIn onSignedIn={() => setAuth('signed-in')} />
  return <AppShell initialView="chat" onSignOut={signOut} />
}

function Unreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <main className="signin">
      <div className="signin-card">
        <span className="mark signin-mark away" aria-hidden="true" />
        <h1 className="signin-title compact">Can't reach Hearthkeep</h1>
        <p className="signin-lede">Check that Hearthkeep is running on your computer, then try again.</p>
        <div className="signin-actions">
          <button type="button" className="btn primary lg block" onClick={onRetry}>
            Retry
          </button>
        </div>
      </div>
    </main>
  )
}
