import { useEffect, useRef, useState, type FormEvent } from 'react'
import { isApiError } from '../api/client'
import { enroll, login, normaliseCode, passkeyErrorMessage } from '../api/webauthn'
import { AlertIcon, BackIcon, InfoIcon, PasskeyIcon } from '../shell/icons'

type Mode = 'signin' | 'enroll'

/** A setup link from another device: `#enroll=` and the 8-character code. */
const ENROLL_LINK = /^#enroll=([A-HJKMNP-Z2-9]{8})$/i

function codeFromLink(): string | null {
  const match = ENROLL_LINK.exec(window.location.hash)
  return match ? match[1].toUpperCase() : null
}

/** A friendly default name; the owner can change it before adding the device. */
export function guessDeviceName(ua = navigator.userAgent, touch = navigator.maxTouchPoints ?? 0): string {
  if (/iPhone/.test(ua)) return 'iPhone'
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && touch > 1)) return 'iPad'
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? 'Android phone' : 'Android tablet'
  if (/Macintosh|Mac OS X/.test(ua)) return 'Mac'
  if (/Windows/.test(ua)) return 'Windows PC'
  if (/CrOS/.test(ua)) return 'Chromebook'
  if (/Linux/.test(ua)) return 'Linux computer'
  return 'This device'
}

function Note({ kind, children }: { kind: 'error' | 'info'; children: string }) {
  const Icon = kind === 'error' ? AlertIcon : InfoIcon
  return (
    <div className={`note ${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      <Icon />
      <span>{children}</span>
    </div>
  )
}

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [linkCode] = useState(codeFromLink)
  const [mode, setMode] = useState<Mode>(linkCode ? 'enroll' : 'signin')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [code, setCode] = useState(linkCode ?? '')
  const [codeInvalid, setCodeInvalid] = useState(false)
  const [deviceName, setDeviceName] = useState(guessDeviceName)
  const codeInput = useRef<HTMLInputElement>(null)
  const signInButton = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (mode === 'enroll') codeInput.current?.focus()
  }, [mode])

  // The code is in the form now; it does not stay in the address bar or the history.
  useEffect(() => {
    if (!window.location.hash.startsWith('#enroll=')) return
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }, [])

  function switchTo(next: Mode, message: string | null = null) {
    setMode(next)
    setError(null)
    setNotice(message)
    setCodeInvalid(false)
    if (next === 'signin') requestAnimationFrame(() => signInButton.current?.focus())
  }

  async function signIn() {
    setBusy(true)
    setError(null)
    try {
      await login()
      onSignedIn()
      return
    } catch (e) {
      if (isApiError(e, 'no_devices')) {
        switchTo('enroll', 'No passkey is set up yet. Enter the setup code from your computer to set up this device.')
      } else {
        setError(passkeyErrorMessage(e, 'Sign-in'))
      }
    }
    setBusy(false)
  }

  async function addDevice(event: FormEvent) {
    event.preventDefault()
    if (!normaliseCode(code)) {
      setCodeInvalid(true)
      setError('Enter the setup code.')
      codeInput.current?.focus()
      return
    }
    setBusy(true)
    setError(null)
    setCodeInvalid(false)
    try {
      await enroll(code, deviceName.trim() || guessDeviceName())
      onSignedIn()
      return
    } catch (e) {
      setCodeInvalid(isApiError(e, 'invalid_code'))
      setError(passkeyErrorMessage(e, 'Setup'))
    }
    setBusy(false)
  }

  return (
    <main className="signin">
      <div className="signin-card" key={mode}>
        <span className="mark signin-mark" aria-hidden="true" />
        {mode === 'signin' ? (
          <>
            <h1 className="signin-title">Hearthkeep</h1>
            <p className="signin-lede">Your private assistant, at home on your own computer.</p>
            <div className="signin-actions">
              {error && <Note kind="error">{error}</Note>}
              <button
                ref={signInButton}
                type="button"
                className="btn primary lg block"
                onClick={signIn}
                disabled={busy}
                aria-busy={busy}
              >
                <PasskeyIcon />
                {busy ? 'Waiting for your passkey…' : 'Sign in with passkey'}
              </button>
              <button type="button" className="btn ghost" onClick={() => switchTo('enroll')} disabled={busy}>
                Set up this device with a code
              </button>
            </div>
          </>
        ) : (
          <>
            <h1 className="signin-title compact">Set up this device</h1>
            <p className="signin-lede">Add a passkey so this device can open Hearthkeep with Face ID, Touch ID or your PIN.</p>
            <form className="signin-form" onSubmit={addDevice} noValidate>
              {notice && <Note kind="info">{notice}</Note>}
              <label className="field">
                <span>Setup code</span>
                <input
                  ref={codeInput}
                  className="input code"
                  name="code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  autoComplete="one-time-code"
                  autoCapitalize="characters"
                  autoCorrect="off"
                  spellCheck={false}
                  inputMode="text"
                  maxLength={16}
                  aria-invalid={codeInvalid}
                  aria-describedby="code-hint"
                />
              </label>
              <p id="code-hint" className="hint">
                Shown on your computer when Hearthkeep starts. Adding another device? Create a code on one that is
                already signed in.
              </p>
              <label className="field">
                <span>Device name</span>
                <input
                  className="input"
                  name="device"
                  value={deviceName}
                  onChange={(e) => setDeviceName(e.target.value)}
                  autoComplete="off"
                  maxLength={200}
                  aria-describedby="device-hint"
                />
              </label>
              <p id="device-hint" className="hint">
                So you can tell your devices apart later.
              </p>
              {error && <Note kind="error">{error}</Note>}
              <button type="submit" className="btn primary lg block" disabled={busy} aria-busy={busy}>
                {busy ? 'Waiting for your passkey…' : 'Add this device'}
              </button>
              <button type="button" className="btn ghost" onClick={() => switchTo('signin')} disabled={busy}>
                <BackIcon />
                Back to sign in
              </button>
            </form>
          </>
        )}
      </div>
      <p className="signin-foot">Runs on your own computer. Nothing leaves it without asking you first.</p>
    </main>
  )
}
