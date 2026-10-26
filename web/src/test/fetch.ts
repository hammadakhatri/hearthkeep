import { vi } from 'vitest'

export type Reply = { status?: number; body?: unknown }
export type Handler = (init: RequestInit) => Reply | Promise<Reply>
export type Call = { path: string; init: RequestInit }

/** Stub global fetch with one handler per "METHOD /path"; unknown routes answer 404. */
export function mockFetch(routes: Record<string, Handler | Reply>) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url
    const method = (init.method ?? 'GET').toUpperCase()
    calls.push({ path, init })
    const route = routes[`${method} ${path}`]
    const reply =
      typeof route === 'function' ? await route(init) : (route ?? { status: 404, body: { reason: 'not_found' } })
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

export function bodyOf(call: Call): Record<string, unknown> {
  return JSON.parse(String(call.init.body))
}

export function headerOf(call: Call, name: string): string | null {
  return new Headers(call.init.headers).get(name)
}

export const healthy = {
  body: { status: 'ok', chat_model: 'Local model 12B', chat_model_reachable: true },
}
export const unreachable = {
  body: { status: 'ok', chat_model: 'Local model 12B', chat_model_reachable: false },
}
export const signedOut = { status: 401, body: { reason: 'unauthenticated' } }
export const signedIn = {
  body: { device: { id: 'cred-1', device_name: 'Laptop', created_at: '2026-10-26T00:00:00Z', current: true } },
}
