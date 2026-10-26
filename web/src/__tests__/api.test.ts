import { describe, expect, it, vi } from 'vitest'
import { startAuthentication } from '@simplewebauthn/browser'
import { ApiError, api } from '../api/client'
import { stepUp } from '../api/webauthn'
import { bodyOf, headerOf, mockFetch } from '../test/fetch'

vi.mock('@simplewebauthn/browser', () => ({
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
  browserSupportsWebAuthn: () => true,
}))

describe('api client', () => {
  it('sends the CSRF header, same-origin credentials and JSON content type', async () => {
    const { calls } = mockFetch({ 'POST /api/thing': { body: { ok: true } } })
    await expect(api('/api/thing', { method: 'POST', body: JSON.stringify({ a: 1 }) })).resolves.toEqual({
      ok: true,
    })
    expect(headerOf(calls[0], 'X-Hearthkeep')).toBe('1')
    expect(headerOf(calls[0], 'Content-Type')).toBe('application/json')
    expect(calls[0].init.credentials).toBe('same-origin')
  })

  it('throws ApiError with the status and reason from the server', async () => {
    mockFetch({ 'GET /api/thing': { status: 409, body: { reason: 'already_resolved' } } })
    const error = await api('/api/thing').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, reason: 'already_resolved' })
  })

  it('reports a network failure as status 0, reason "network"', async () => {
    mockFetch({ 'GET /api/thing': () => Promise.reject(new TypeError('Failed to fetch')) })
    await expect(api('/api/thing')).rejects.toMatchObject({ status: 0, reason: 'network' })
  })

  it('announces an expired session', async () => {
    mockFetch({ 'GET /api/thing': { status: 401, body: { reason: 'unauthenticated' } } })
    const listener = vi.fn()
    window.addEventListener('hearthkeep:unauthenticated', listener)
    await api('/api/thing').catch(() => {})
    window.removeEventListener('hearthkeep:unauthenticated', listener)
    expect(listener).toHaveBeenCalledOnce()
  })
})

describe('stepUp', () => {
  it('runs the passkey ceremony for the purpose and returns the step-up token', async () => {
    const options = { challenge: 'c3RlcA', allowCredentials: [{ id: 'cred-1', type: 'public-key' }] }
    const assertion = { id: 'cred-1', response: {} }
    vi.mocked(startAuthentication).mockResolvedValue(assertion as never)
    const { calls } = mockFetch({
      'POST /api/auth/stepup/options': { body: { challenge_id: 'ch-9', options } },
      'POST /api/auth/stepup/verify': { body: { step_up_token: 'token-xyz' } },
    })
    await expect(stepUp('approval', 'appr-1')).resolves.toBe('token-xyz')
    expect(startAuthentication).toHaveBeenCalledWith({ optionsJSON: options })
    expect(bodyOf(calls[0])).toEqual({ purpose: 'approval', approval_id: 'appr-1' })
    expect(bodyOf(calls[1])).toEqual({
      purpose: 'approval',
      approval_id: 'appr-1',
      challenge_id: 'ch-9',
      response: assertion,
    })
  })

  it('omits approval_id for other purposes', async () => {
    vi.mocked(startAuthentication).mockResolvedValue({ id: 'cred-1' } as never)
    const { calls } = mockFetch({
      'POST /api/auth/stepup/options': { body: { challenge_id: 'ch-9', options: { challenge: 'eA' } } },
      'POST /api/auth/stepup/verify': { body: { step_up_token: 't' } },
    })
    await stepUp('settings')
    expect(bodyOf(calls[0])).toEqual({ purpose: 'settings' })
  })
})
