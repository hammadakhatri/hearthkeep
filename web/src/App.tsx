import { useEffect, useState } from 'react'
import { api, UNAUTHENTICATED_EVENT } from './api/client'
import { AppShell } from './shell/AppShell'
import { SignIn } from './views/SignIn'

type Auth = 'checking' | 'signed-out' | 'signed-in'

export default function App() {
  const [auth, setAuth] = useState<Auth>('checking')

  useEffect(() => {
    let alive = true
    api('/api/auth/session')
      .then(() => alive && setAuth('signed-in'))
      .catch(() => alive && setAuth('signed-out'))
    const expired = () => setAuth('signed-out')
    window.addEventListener(UNAUTHENTICATED_EVENT, expired)
    return () => {
      alive = false
      window.removeEventListener(UNAUTHENTICATED_EVENT, expired)
    }
  }, [])

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
  if (auth === 'signed-out') return <SignIn onSignedIn={() => setAuth('signed-in')} />
  return <AppShell initialView="chat" onSignOut={signOut} />
}
