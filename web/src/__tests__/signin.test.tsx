import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { startAuthentication, startRegistration } from '@simplewebauthn/browser'
import App from '../App'
import { bodyOf, headerOf, healthy, mockFetch, signedIn, signedOut } from '../test/fetch'

vi.mock('@simplewebauthn/browser', () => ({
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
  browserSupportsWebAuthn: () => true,
}))

const loginOptions = { challenge: 'Y2hhbGxlbmdl', allowCredentials: [{ id: 'cred-1', type: 'public-key' }] }
const registerOptions = { challenge: 'cmVnaXN0ZXI', user: { id: 'b3duZXI', name: 'owner', displayName: 'Owner' } }
const assertion = { id: 'cred-1', rawId: 'cred-1', type: 'public-key', response: {}, clientExtensionResults: {} }

beforeEach(() => {
  vi.mocked(startAuthentication).mockReset().mockResolvedValue(assertion as never)
  vi.mocked(startRegistration).mockReset().mockResolvedValue(assertion as never)
})

describe('Sign in', () => {
  it('sign-in shows enrollment form when server says no devices', async () => {
    mockFetch({
      'GET /api/auth/session': signedOut,
      'GET /api/health': healthy,
      'POST /api/auth/login/options': { status: 409, body: { reason: 'no_devices' } },
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with passkey' }))
    expect(await screen.findByLabelText('Setup code')).toBeInTheDocument()
    expect(screen.getByLabelText('Device name')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add this device' })).toBeInTheDocument()
    expect(startAuthentication).not.toHaveBeenCalled()
  })

  it('sign-in calls login() and routes to Chat on success', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/session': signedOut,
      'GET /api/health': healthy,
      'POST /api/auth/login/options': { body: { challenge_id: 'ch-1', options: loginOptions } },
      'POST /api/auth/login/verify': signedIn,
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with passkey' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument()

    expect(startAuthentication).toHaveBeenCalledWith({ optionsJSON: loginOptions })
    const verify = calls.find((c) => c.path === '/api/auth/login/verify')!
    expect(bodyOf(verify)).toEqual({ challenge_id: 'ch-1', response: assertion })
    expect(headerOf(verify, 'X-Hearthkeep')).toBe('1')
    expect(verify.init.credentials).toBe('same-origin')
  })

  it('opens straight into the app when a session already exists', async () => {
    mockFetch({ 'GET /api/auth/session': signedIn, 'GET /api/health': healthy })
    render(<App />)
    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument()
  })

  it('returns to sign-in when the session expires', async () => {
    mockFetch({ 'GET /api/auth/session': signedIn, 'GET /api/health': healthy })
    render(<App />)
    await screen.findByRole('heading', { level: 1, name: 'Chat' })
    window.dispatchEvent(new Event('hearthkeep:unauthenticated'))
    expect(await screen.findByRole('button', { name: 'Sign in with passkey' })).toBeInTheDocument()
  })

  it('shows a plain message when rate limited', async () => {
    mockFetch({
      'GET /api/auth/session': signedOut,
      'POST /api/auth/login/options': { status: 429, body: { reason: 'rate_limited' } },
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Wait a minute and try again.')
  })

  it('stays on sign-in when the passkey prompt is cancelled', async () => {
    mockFetch({
      'GET /api/auth/session': signedOut,
      'POST /api/auth/login/options': { body: { challenge_id: 'ch-1', options: loginOptions } },
    })
    vi.mocked(startAuthentication).mockRejectedValue(
      Object.assign(new Error('cancelled'), { name: 'NotAllowedError' }),
    )
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-in was cancelled.')
    expect(screen.getByRole('button', { name: 'Sign in with passkey' })).toBeEnabled()
  })

  it('enrolls this device with the code and device name, then opens Chat', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/session': signedOut,
      'GET /api/health': healthy,
      'POST /api/auth/enroll/options': { body: { challenge_id: 'ch-2', options: registerOptions } },
      'POST /api/auth/enroll/verify': { status: 201, body: signedIn.body },
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set up this device with a code' }))
    fireEvent.change(screen.getByLabelText('Setup code'), { target: { value: 'abcd 2345' } })
    fireEvent.change(screen.getByLabelText('Device name'), { target: { value: 'Kitchen tablet' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add this device' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument()

    expect(bodyOf(calls.find((c) => c.path === '/api/auth/enroll/options')!)).toEqual({ code: 'ABCD2345' })
    expect(startRegistration).toHaveBeenCalledWith({ optionsJSON: registerOptions })
    expect(bodyOf(calls.find((c) => c.path === '/api/auth/enroll/verify')!)).toEqual({
      code: 'ABCD2345',
      challenge_id: 'ch-2',
      response: assertion,
      device_name: 'Kitchen tablet',
    })
  })

  it('explains an invalid setup code', async () => {
    mockFetch({
      'GET /api/auth/session': signedOut,
      'POST /api/auth/enroll/options': { status: 400, body: { reason: 'invalid_code' } },
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set up this device with a code' }))
    fireEvent.change(screen.getByLabelText('Setup code'), { target: { value: 'WRONG234' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add this device' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("That code didn't work.")
    expect(startRegistration).not.toHaveBeenCalled()
  })
})
