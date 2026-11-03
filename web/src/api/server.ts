/** The running server (version, restart, stop), waiting for it to come back, and the one-time
 *  "Hearthkeep was updated" note. */

import type { AddonId } from './addons'
import { api, post } from './client'

export type ServerInfo = {
  version: string
  supervised: boolean
  setup_mode: boolean
  started_at: string
  restart_needed: boolean
  restart_reasons: string[]
}

export function getServer(signal?: AbortSignal): Promise<ServerInfo> {
  return api<ServerInfo>('/api/server', { signal })
}

/** Whether the owner stopped Hearthkeep from this page. Kept for the page's life, so Settings
 *  still says so after the owner leaves it and comes back, when the server can't answer. */
export const serverSession = { stopped: false }

/** Needs a step-up token (purpose `server`). 409 `not_supervised` when nothing would start it again. */
export function restartServer(token: string): Promise<{ ok: true }> {
  return api('/api/server/restart', post({ step_up_token: token }))
}

/** Needs a step-up token (purpose `server`). */
export function stopServer(token: string): Promise<{ ok: true }> {
  return api('/api/server/stop', post({ step_up_token: token }))
}

export type WaitOptions = { timeoutMs?: number; intervalMs?: number }

/** Ask `check` until it says yes or the time is up. A failed ask (the server is between
 *  processes) is a no, never the end, and each ask has its own time limit, so one that hangs
 *  can't keep the wait going forever. */
async function waitUntil(
  check: (signal: AbortSignal) => Promise<boolean>,
  { timeoutMs = 60_000, intervalMs = 1000 }: WaitOptions,
) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    try {
      if (await check(AbortSignal.timeout(intervalMs * 3))) return true
    } catch {
      // Down for a moment while it restarts or hands over: ask again.
    }
    if (Date.now() + intervalMs > deadline) return false
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs))
  }
}

/** True once the server answers with another `started_at` than `previous`. */
export function waitForRestart(previous: string, options: WaitOptions = {}): Promise<boolean> {
  return waitUntil(async (signal) => (await getServer(signal)).started_at !== previous, options)
}

/** True once `/api/health` answers from the real server, out of setup mode. */
export function waitForSetupDone(options: WaitOptions = {}): Promise<boolean> {
  return waitUntil(
    async (signal) => (await api<{ setup_mode?: boolean }>('/api/health', { signal })).setup_mode === false,
    options,
  )
}

/** Page loads, behind one object so tests can watch them (jsdom's `location` can't be stubbed). */
export const page = {
  reload(): void {
    window.location.reload()
  },
  replace(url: string): void {
    window.location.replace(url)
  },
}

export type UpdateNoteItem = { id: string; text: string; addon: AddonId | null }
export type UpdateNote = { version: string; items: UpdateNoteItem[] }

export function getUpdateNote(): Promise<{ note: UpdateNote | null }> {
  return api('/api/update-note')
}

export function dismissUpdateNote(): Promise<{ ok: true }> {
  return api('/api/update-note/dismiss', { method: 'POST' })
}
