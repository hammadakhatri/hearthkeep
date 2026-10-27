import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import swSource from '../../public/sw.js?raw'
import { NotificationToggle } from '../components/NotificationToggle'
import { pushSupport } from '../push'
import { bodyOf, mockFetch } from '../test/fetch'

const ANDROID_CHROME =
  'Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36'
const SAMSUNG_INTERNET =
  'Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36'
const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
const IPAD_DESKTOP_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'

// 65 bytes: 0x04 then 64 bytes of 1..64, base64url without padding.
const POINT = Uint8Array.from([4, ...Array.from({ length: 64 }, (_, i) => i + 1)])
const PUBLIC_KEY = btoa(String.fromCharCode(...POINT)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const ENDPOINT = 'https://fcm.googleapis.com/fcm/send/device-token'

const HINT = 'Get a notice here when something is sent to this device. It says only "A file is ready in Hearthkeep".'
const IPHONE_NOTE = 'On iPhone, add Hearthkeep to your Home Screen first, then turn this on there.'
const BLOCKED = "Notifications are blocked for this site. Allow them in the browser's site settings."

type FakeSubscription = { endpoint: string; options: { applicationServerKey: ArrayBuffer | null }; unsubscribe: ReturnType<typeof vi.fn> }

function fakeBrowser({
  ua = ANDROID_CHROME,
  permission = 'default' as NotificationPermission,
  answer = 'granted' as NotificationPermission,
  subscription = null as FakeSubscription | null,
  standalone = false,
  touchPoints = 5,
  push = true,
} = {}) {
  let current = subscription
  const unsubscribe = vi.fn(async () => {
    current = null
    return true
  })
  if (current) current.unsubscribe = unsubscribe
  const pushManager = {
    getSubscription: vi.fn(async () => current),
    subscribe: vi.fn(async (options: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }) => {
      const key = options.applicationServerKey
      current = {
        endpoint: ENDPOINT,
        options: { applicationServerKey: key.buffer.slice(key.byteOffset, key.byteOffset + key.byteLength) as ArrayBuffer },
        unsubscribe,
      }
      return current
    }),
  }
  const notification = {
    permission,
    requestPermission: vi.fn(async () => {
      notification.permission = answer
      return answer
    }),
  }
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(ua)
  Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, value: touchPoints })
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: { ready: Promise.resolve({ pushManager }), register: vi.fn(async () => ({ pushManager })) },
  })
  if (push) vi.stubGlobal('PushManager', function PushManager() {})
  else Reflect.deleteProperty(globalThis, 'PushManager')
  vi.stubGlobal('Notification', notification)
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: standalone && query === '(display-mode: standalone)' }))
  return { pushManager, notification, unsubscribe }
}

afterEach(() => {
  Reflect.deleteProperty(navigator, 'serviceWorker')
  Reflect.deleteProperty(navigator, 'maxTouchPoints')
  Reflect.deleteProperty(globalThis, 'PushManager')
})

describe('push support', () => {
  it('Android Chrome and Samsung Internet are supported', () => {
    fakeBrowser({ ua: ANDROID_CHROME })
    expect(pushSupport()).toBe('supported')
    fakeBrowser({ ua: SAMSUNG_INTERNET })
    expect(pushSupport()).toBe('supported')
  })

  it('iPhone and iPad Safari need the Home Screen unless running standalone', () => {
    fakeBrowser({ ua: IPHONE_SAFARI, push: false })
    expect(pushSupport()).toBe('needs-home-screen')
    fakeBrowser({ ua: IPAD_DESKTOP_UA, push: false, touchPoints: 5 })
    expect(pushSupport()).toBe('needs-home-screen')
    fakeBrowser({ ua: IPHONE_SAFARI, standalone: true })
    expect(pushSupport()).toBe('supported')
  })

  it('no PushManager is unsupported', () => {
    fakeBrowser({ push: false })
    expect(pushSupport()).toBe('unsupported')
  })
})

describe('NotificationToggle', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('toggle subscribes with the server key and saves the endpoint', async () => {
    const { pushManager, notification } = fakeBrowser()
    const { calls } = mockFetch({
      'GET /api/push': { body: { public_key: PUBLIC_KEY, enabled: false } },
      'PUT /api/push/subscription': { body: { enabled: true } },
    })
    render(<NotificationToggle />)
    const toggle = await screen.findByRole('switch', { name: 'Notifications on this device' })
    expect(toggle).toHaveAccessibleDescription(HINT)
    await waitFor(() => expect(toggle).toBeEnabled())
    expect(toggle).toHaveAttribute('aria-checked', 'false')

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    expect(notification.requestPermission).toHaveBeenCalledOnce()
    expect(pushManager.subscribe).toHaveBeenCalledOnce()
    const options = pushManager.subscribe.mock.calls[0][0]
    expect(options.userVisibleOnly).toBe(true)
    expect(Array.from(options.applicationServerKey)).toEqual(Array.from(POINT))
    const put = calls.find((c) => c.init.method === 'PUT')!
    expect(put.path).toBe('/api/push/subscription')
    expect(bodyOf(put)).toEqual({ endpoint: ENDPOINT })
  })

  it('toggle off unsubscribes', async () => {
    const existing: FakeSubscription = {
      endpoint: ENDPOINT,
      options: { applicationServerKey: POINT.buffer.slice(0) },
      unsubscribe: vi.fn(),
    }
    const { unsubscribe } = fakeBrowser({ permission: 'granted', subscription: existing })
    const { calls } = mockFetch({
      'GET /api/push': { body: { public_key: PUBLIC_KEY, enabled: true } },
      'DELETE /api/push/subscription': { body: { enabled: false } },
    })
    render(<NotificationToggle />)
    const toggle = await screen.findByRole('switch', { name: 'Notifications on this device' })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    expect(calls.some((c) => c.init.method === 'DELETE' && c.path === '/api/push/subscription')).toBe(true)
    expect(unsubscribe).toHaveBeenCalledOnce()
  })

  it('shows the home screen note on iPhone Safari', async () => {
    fakeBrowser({ ua: IPHONE_SAFARI, push: false })
    const { calls } = mockFetch({ 'GET /api/push': { body: { public_key: PUBLIC_KEY, enabled: false } } })
    render(<NotificationToggle />)
    expect(await screen.findByText(IPHONE_NOTE)).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Notifications on this device' })).toBeDisabled()
    expect(calls).toEqual([])
  })

  it('says when notifications are blocked', async () => {
    const { pushManager } = fakeBrowser({ answer: 'denied' })
    const { calls } = mockFetch({ 'GET /api/push': { body: { public_key: PUBLIC_KEY, enabled: false } } })
    render(<NotificationToggle />)
    const toggle = await screen.findByRole('switch', { name: 'Notifications on this device' })
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    expect(await screen.findByText(BLOCKED)).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(pushManager.subscribe).not.toHaveBeenCalled()
    expect(calls.some((c) => c.init.method === 'PUT')).toBe(false)
  })

  it('already blocked shows the note and keeps the switch off', async () => {
    fakeBrowser({ permission: 'denied' })
    mockFetch({ 'GET /api/push': { body: { public_key: PUBLIC_KEY, enabled: true } } })
    render(<NotificationToggle />)
    expect(await screen.findByText(BLOCKED)).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Notifications on this device' })).toHaveAttribute('aria-checked', 'false')
  })
})

type Listener = (event: unknown) => void

function workerScope(windows: { url: string; focus: ReturnType<typeof vi.fn>; postMessage: ReturnType<typeof vi.fn> }[] = []) {
  const listeners: Record<string, Listener> = {}
  const scope = {
    location: new URL('https://hearthkeep.example/sw.js'),
    addEventListener: (type: string, listener: Listener) => {
      listeners[type] = listener
    },
    registration: { showNotification: vi.fn(async () => {}) },
    clients: {
      matchAll: vi.fn(async () => windows),
      openWindow: vi.fn(async () => null),
      claim: vi.fn(async () => {}),
    },
    skipWaiting: vi.fn(async () => {}),
  }
  new Function('self', swSource)(scope)
  return { scope, listeners }
}

describe('service worker', () => {
  it('service worker shows the content-free notice', async () => {
    const { scope, listeners } = workerScope()
    const read = vi.fn()
    const waits: Promise<unknown>[] = []
    const event = {
      get data() {
        read()
        return { text: () => 'leaked', json: () => ({ body: 'leaked' }) }
      },
      waitUntil: (p: Promise<unknown>) => waits.push(p),
    }
    listeners.push(event)
    await Promise.all(waits)
    expect(waits).toHaveLength(1)
    expect(read).not.toHaveBeenCalled()
    expect(scope.registration.showNotification).toHaveBeenCalledWith('Hearthkeep', {
      body: 'A file is ready in Hearthkeep',
      tag: 'hearthkeep-sent',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
    })
  })

  it('a click focuses an open window and asks it to open the tray', async () => {
    const win = { url: 'https://hearthkeep.example/#chat', focus: vi.fn(async () => win), postMessage: vi.fn() }
    const { scope, listeners } = workerScope([win])
    const close = vi.fn()
    const waits: Promise<unknown>[] = []
    listeners.notificationclick({ notification: { close }, waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
    expect(close).toHaveBeenCalledOnce()
    expect(win.focus).toHaveBeenCalledOnce()
    expect(win.postMessage).toHaveBeenCalledWith({ type: 'open-sent' })
    expect(scope.clients.openWindow).not.toHaveBeenCalled()
  })

  it('a click with no open window opens the tray in a new one', async () => {
    const other = { url: 'https://elsewhere.example/', focus: vi.fn(), postMessage: vi.fn() }
    const { scope, listeners } = workerScope([other])
    const waits: Promise<unknown>[] = []
    listeners.notificationclick({ notification: { close: vi.fn() }, waitUntil: (p: Promise<unknown>) => waits.push(p) })
    await Promise.all(waits)
    expect(scope.clients.openWindow).toHaveBeenCalledWith('/#sent')
    expect(other.postMessage).not.toHaveBeenCalled()
  })

  it('never reads the push data or decrypts anything', () => {
    expect(swSource).not.toMatch(/\.data\b/)
    expect(swSource).not.toMatch(/import|importScripts|decrypt/)
  })
})
