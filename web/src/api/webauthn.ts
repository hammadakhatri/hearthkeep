/** Passkey ceremonies: set up this device, sign in, and step up for sensitive actions. */

import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from '@simplewebauthn/browser'
import { api, isApiError, post } from './client'

export type StepUpPurpose = 'approval' | 'settings' | 'add_device'

export type Device = { id: string; device_name: string; created_at: string; current: boolean }

type Ceremony<O> = { challenge_id: string; options: O & { insecureDevAuth?: unknown } }

/* `hearthkeep serve --insecure-dev-auth` (localhost only) marks its options with
 * `insecureDevAuth: true` and accepts any response, so no authenticator is involved.
 * Only the exact boolean counts: anything else runs the real ceremony. */
function isDevAuth(options: { insecureDevAuth?: unknown }): boolean {
  return options.insecureDevAuth === true
}

function devCredentialId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function register(options: Ceremony<PublicKeyCredentialCreationOptionsJSON>['options']) {
  if (isDevAuth(options)) return { id: devCredentialId(), type: 'public-key', response: {} }
  return startRegistration({ optionsJSON: options })
}

async function assert(options: Ceremony<PublicKeyCredentialRequestOptionsJSON>['options']) {
  if (isDevAuth(options)) {
    return { id: options.allowCredentials?.[0]?.id ?? '', type: 'public-key', response: {} }
  }
  return startAuthentication({ optionsJSON: options })
}

/** Normalise a setup code the way it is printed: upper case, no spaces or dashes. */
export function normaliseCode(code: string): string {
  return code.replace(/[\s-]+/g, '').toUpperCase()
}

/** Register a passkey on this device with a setup code; signs this browser in. */
export async function enroll(code: string, deviceName: string): Promise<Device> {
  const clean = normaliseCode(code)
  const { challenge_id, options } = await api<Ceremony<PublicKeyCredentialCreationOptionsJSON>>(
    '/api/auth/enroll/options',
    post({ code: clean }),
  )
  const response = await register(options)
  const result = await api<{ device: Device }>(
    '/api/auth/enroll/verify',
    post({ code: clean, challenge_id, response, device_name: deviceName.trim() }),
  )
  return result.device
}

/** Sign in with a passkey. Throws ApiError 409 "no_devices" when nothing is set up yet. */
export async function login(): Promise<Device> {
  const { challenge_id, options } = await api<Ceremony<PublicKeyCredentialRequestOptionsJSON>>(
    '/api/auth/login/options',
    post({}),
  )
  const response = await assert(options)
  const result = await api<{ device: Device }>('/api/auth/login/verify', post({ challenge_id, response }))
  return result.device
}

/** Confirm a sensitive action with this session's passkey; returns the step-up token. */
export async function stepUp(purpose: StepUpPurpose, approvalId?: string): Promise<string> {
  const target = approvalId === undefined ? { purpose } : { purpose, approval_id: approvalId }
  const { challenge_id, options } = await api<Ceremony<PublicKeyCredentialRequestOptionsJSON>>(
    '/api/auth/stepup/options',
    post(target),
  )
  const response = await assert(options)
  const result = await api<{ step_up_token: string }>(
    '/api/auth/stepup/verify',
    post({ ...target, challenge_id, response }),
  )
  return result.step_up_token
}

const REASONS: Record<string, string> = {
  rate_limited: 'Too many attempts. Wait a minute and try again.',
  invalid_code: "That code didn't work. Codes work once and expire after 15 minutes.",
  verification_failed: "Your passkey couldn't be confirmed. Try again.",
  challenge_invalid: 'That took too long. Try again.',
  credential_exists: 'This passkey is already set up. Sign in instead.',
  network: "Can't reach Hearthkeep. Check that it's running on your computer.",
}

/** A plain sentence for anything a passkey ceremony can throw. */
export function passkeyErrorMessage(error: unknown, action = 'Sign-in'): string {
  if (isApiError(error)) return REASONS[error.reason] ?? 'Something went wrong. Try again.'
  const name = error instanceof Error ? error.name : ''
  if (name === 'NotAllowedError' || name === 'AbortError') return `${action} was cancelled.`
  if (name === 'InvalidStateError') return 'This device already has a passkey for Hearthkeep. Sign in instead.'
  if (!browserSupportsWebAuthn()) return "This browser can't use passkeys. Try Safari, Chrome or Edge."
  return "Your passkey couldn't be used. Try again."
}
