import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { startRegistration } from '@simplewebauthn/browser'
import App from '../App'
import { page, waitForSetupDone } from '../api/server'
import { readSetupSecret } from '../api/setup'
import { forbiddenIn } from '../test/copyRules'
import { bodyOf, healthy, mockFetch, signedIn, signedOut, type Handler, type Reply } from '../test/fetch'
import { SetupPage } from '../views/SetupPage'

vi.mock('@simplewebauthn/browser', () => ({
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
  browserSupportsWebAuthn: () => true,
}))

vi.mock('../api/server', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/server')>()
  return { ...actual, waitForSetupDone: vi.fn(actual.waitForSetupDone) }
})

const SECRET = 'a'.repeat(43)
const CODE = 'ABCDEFGH'
const registerOptions = { challenge: 'cmVnaXN0ZXI', user: { id: 'b3duZXI', name: 'owner', displayName: 'Owner' } }
const attestation = { id: 'cred-1', rawId: 'cred-1', type: 'public-key', response: {}, clientExtensionResults: {} }
const BUSY = 'Busy for a moment, trying again…'
const ENDED = 'This setup link has ended. Double-click Setup.cmd in the Hearthkeep folder to start again.'

function setupRoutes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'POST /api/setup/status': { body: { state: 'needs_passkey' } },
    'POST /api/setup/enrollment-code': { body: { code: CODE, expires_in_minutes: 15 } },
    'POST /api/auth/enroll/options': { body: { challenge_id: 'ch-1', options: registerOptions } },
    'POST /api/auth/enroll/verify': signedIn,
    'GET /api/health': { body: { ...healthy.body, setup_mode: false } },
    ...extra,
  })
}

let replace: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  window.history.replaceState(null, '', `/setup#${SECRET}`)
  vi.mocked(startRegistration).mockReset().mockResolvedValue(attestation as never)
  replace = vi.spyOn(page, 'replace').mockImplementation(() => {})
})

afterEach(() => {
  replace.mockRestore()
  window.history.replaceState(null, '', '/')
})

describe('Setup page', () => {
  it('reads the secret, clears it from the address bar, and creates the passkey with the fetched code', async () => {
    const logged = [vi.spyOn(console, 'log'), vi.spyOn(console, 'info'), vi.spyOn(console, 'warn'), vi.spyOn(console, 'error')]
    const { calls } = setupRoutes()
    render(<App />)
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/setup')

    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText('Finishing setup…')).toHaveAttribute('role', 'status')
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'))

    const status = calls.find((c) => c.path === '/api/setup/status')!
    expect(bodyOf(status)).toEqual({ secret: SECRET })
    const code = calls.find((c) => c.path === '/api/setup/enrollment-code')!
    expect(bodyOf(code)).toEqual({ secret: SECRET })
    expect(code.init.method).toBe('POST')
    const options = calls.find((c) => c.path === '/api/auth/enroll/options')!
    expect(bodyOf(options)).toEqual({ code: CODE })
    expect(bodyOf(calls.find((c) => c.path === '/api/auth/enroll/verify')!)).toMatchObject({ device_name: expect.any(String) })
    // The session check waits until setup is over.
    expect(calls.some((c) => c.path === '/api/auth/session')).toBe(false)

    for (const call of calls) {
      expect(call.path).not.toContain(SECRET)
      expect(call.path).not.toContain(CODE)
    }
    expect(document.body.innerHTML).not.toContain(CODE)
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(window.location.href).not.toContain(SECRET)
    expect(JSON.stringify({ ...window.localStorage, ...window.sessionStorage })).not.toContain(SECRET)
    for (const spy of logged) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(SECRET)
      expect(JSON.stringify(spy.mock.calls)).not.toContain(CODE)
      spy.mockRestore()
    }
  })

  it('reads the secret once under strict mode', async () => {
    setupRoutes()
    render(
      <StrictMode>
        <SetupPage onLeave={() => {}} />
      </StrictMode>,
    )
    expect(await screen.findByRole('button', { name: 'Create passkey' })).toBeInTheDocument()
  })

  it('without a secret it says to double-click Setup.cmd', () => {
    window.history.replaceState(null, '', '/setup')
    const { calls } = setupRoutes()
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: 'Set up Hearthkeep' })).toBeInTheDocument()
    expect(
      screen.getByText('Open setup from this PC: double-click Setup.cmd in the Hearthkeep folder.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull()
    expect(calls).toHaveLength(0)
  })

  it('drops a fragment that is not a secret, and clears it too', () => {
    window.history.replaceState(null, '', '/setup#short')
    expect(readSetupSecret()).toBeNull()
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/setup')
  })

  it('a 404 status leaves for the normal app', async () => {
    setupRoutes({
      'POST /api/setup/status': { status: 404, body: { reason: 'not_found' } },
      'GET /api/auth/session': signedOut,
      'GET /api/health': healthy,
    })
    render(<App />)
    expect(await screen.findByRole('button', { name: 'Sign in with passkey' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
  })

  it('a 403 says the link has ended', async () => {
    setupRoutes({ 'POST /api/setup/status': { status: 403, body: { reason: 'setup_secret' } } })
    render(<SetupPage onLeave={() => {}} />)
    expect(await screen.findByText(ENDED)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull()
  })

  it('a 429 waits and tries again instead of giving up', async () => {
    let asked = 0
    const { calls } = setupRoutes({
      'POST /api/setup/status': () => (++asked === 1 ? { status: 429, body: { reason: 'rate_limited' } } : { body: { state: 'needs_passkey' } }),
      'POST /api/setup/enrollment-code': () =>
        ++asked === 3 ? { status: 429, body: { reason: 'rate_limited' } } : { body: { code: CODE, expires_in_minutes: 15 } },
    })
    render(<SetupPage onLeave={() => {}} retryMs={40} />)
    expect(await screen.findByText(BUSY)).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText(BUSY)).toBeInTheDocument()
    await waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
    expect(screen.queryByText(ENDED)).toBeNull()
    expect(calls.filter((c) => c.path === '/api/setup/enrollment-code')).toHaveLength(2)
  })

  it('a cancelled passkey shows the message and the button again', async () => {
    vi.mocked(startRegistration).mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    setupRoutes()
    render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Setup was cancelled.')
    expect(screen.getByRole('button', { name: 'Create passkey' })).toBeEnabled()
    expect(replace).not.toHaveBeenCalled()
  })

  it('says when Hearthkeep is slow to start after setup', async () => {
    vi.mocked(waitForSetupDone).mockResolvedValueOnce(false)
    setupRoutes()
    render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(
      await screen.findByText('Hearthkeep is taking a while to start. Open it from the Start menu in a minute.'),
    ).toBeInTheDocument()
    expect(replace).not.toHaveBeenCalled()
  })

  it('never shows a command or placeholder', async () => {
    const seen: string[] = []
    // No secret.
    window.history.replaceState(null, '', '/setup')
    const first = render(<SetupPage onLeave={() => {}} />)
    seen.push(first.container.textContent ?? '')
    first.unmount()
    // The link has ended.
    window.history.replaceState(null, '', `/setup#${SECRET}`)
    setupRoutes({ 'POST /api/setup/status': { status: 403, body: { reason: 'setup_secret' } } })
    const ended = render(<SetupPage onLeave={() => {}} />)
    await screen.findByText(ENDED)
    seen.push(ended.container.textContent ?? '')
    ended.unmount()
    // Ready, a cancelled passkey, then slow to start.
    window.history.replaceState(null, '', `/setup#${SECRET}`)
    vi.mocked(startRegistration).mockRejectedValueOnce(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    vi.mocked(waitForSetupDone).mockResolvedValueOnce(false)
    setupRoutes()
    const ready = render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    await screen.findByRole('alert')
    seen.push(ready.container.textContent ?? '')
    fireEvent.click(screen.getByRole('button', { name: 'Create passkey' }))
    await screen.findByText(/taking a while/)
    seen.push(ready.container.textContent ?? '')

    for (const text of seen) {
      expect(forbiddenIn(text)).toEqual([])
    }
  })
})
