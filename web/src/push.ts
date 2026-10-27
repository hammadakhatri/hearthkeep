/**
 * Notifications on this device: payload-free web push. The server only ever learns this
 * browser's push endpoint; the notice itself always reads "A file is ready in Hearthkeep".
 */
import { api } from './api/client'

export type PushSupport = 'supported' | 'needs-home-screen' | 'unsupported'

type PushState = { public_key: string; enabled: boolean }

/** Thrown by `enableNotifications` when the browser's permission is denied. */
export class NotificationsBlockedError extends Error {
  constructor() {
    super('Notifications are blocked for this site')
    this.name = 'NotificationsBlockedError'
  }
}

function isAppleMobile(): boolean {
  const ua = navigator.userAgent
  // iPadOS Safari presents itself as a Mac; a Mac has no touch screen.
  return /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)
}

function isStandalone(): boolean {
  const media = typeof window.matchMedia === 'function' && window.matchMedia('(display-mode: standalone)').matches
  return media || (navigator as Navigator & { standalone?: boolean }).standalone === true
}

/**
 * Android Chrome and Samsung Internet: supported. iPhone/iPad Safari can push only from a
 * Home Screen app, so outside one it needs the Home Screen first. Anything without the
 * service worker and Push APIs: unsupported.
 */
export function pushSupport(): PushSupport {
  if (isAppleMobile() && !isStandalone()) return 'needs-home-screen'
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    return 'unsupported'
  }
  return 'supported'
}

export function notificationsBlocked(): boolean {
  return 'Notification' in window && Notification.permission === 'denied'
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = base64url.replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4))
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

function sameKey(current: ArrayBuffer | null | undefined, key: Uint8Array): boolean {
  if (!current) return false
  const bytes = new Uint8Array(current)
  return bytes.length === key.length && bytes.every((b, i) => b === key[i])
}

async function pushManager(): Promise<PushManager> {
  return (await navigator.serviceWorker.ready).pushManager
}

/** On when this browser has permission and a subscription and the server has this device's. */
export async function notificationsEnabled(): Promise<boolean> {
  if (pushSupport() !== 'supported' || Notification.permission !== 'granted') return false
  const [state, subscription] = await Promise.all([
    api<PushState>('/api/push'),
    pushManager().then((manager) => manager.getSubscription()),
  ])
  return state.enabled && subscription !== null
}

/** Ask permission, subscribe with the server's key and save the endpoint for this device. */
export async function enableNotifications(): Promise<void> {
  if ((await Notification.requestPermission()) !== 'granted') throw new NotificationsBlockedError()
  const { public_key } = await api<PushState>('/api/push')
  const key = keyBytes(public_key)
  const manager = await pushManager()
  let subscription = await manager.getSubscription()
  if (subscription && !sameKey(subscription.options.applicationServerKey, key)) {
    // Made with another server key: the push service would refuse our signature.
    await subscription.unsubscribe()
    subscription = null
  }
  subscription ??= await manager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
  try {
    await api('/api/push/subscription', {
      method: 'PUT',
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    })
  } catch (error) {
    await subscription.unsubscribe().catch(() => false)
    throw error
  }
}

/** Forget this device's endpoint on the server first, then unsubscribe the browser. */
export async function disableNotifications(): Promise<void> {
  await api('/api/push/subscription', { method: 'DELETE' })
  const subscription = await (await pushManager()).getSubscription()
  await subscription?.unsubscribe().catch(() => false)
}
