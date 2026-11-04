/** First-run setup: the secret from the setup link, and the setup endpoints.
 *
 * The secret arrives in the address bar's fragment (`/setup#<secret>`), which never reaches the
 * server. It is read once, taken out of the address bar and the history, and only ever sent in a
 * POST body: never a query string, storage or a log. */

import type { AddonsResponse, SandboxBuildApi, SandboxBuildState } from './addons'
import { api, post } from './client'
import type { Catalog, ChatSwitch, Job, ModelsApi, PcInfo } from './models'
import { waitUntil, type WaitOptions } from './server'

const SECRET = /^#[A-Za-z0-9_-]{20,128}$/

/** The setup secret from the fragment, or null. Any fragment is cleared from the address bar. */
export function readSetupSecret(): string | null {
  const hash = window.location.hash
  if (!hash) return null
  window.history.replaceState(null, '', '/setup')
  return SECRET.test(hash) ? hash.slice(1) : null
}

/** Whether setup is still waiting for the passkey. 404 once setup is over, 403 for a wrong secret. */
export function setupStatus(secret: string): Promise<{ state: 'needs_passkey' }> {
  return api('/api/setup/status', post({ secret }))
}

/** The code for the first passkey. Kept by the caller in a local variable only. */
export function setupEnrollmentCode(secret: string): Promise<{ code: string; expires_in_minutes: number }> {
  return api('/api/setup/enrollment-code', post({ secret }))
}

// ---- the wizard's calls: every one a POST with the secret in the body ----------------------

export type SetupPc = {
  pc: PcInfo
  /** The data folder: the owner's choice or the default, shown as a value. */
  data_dir: string
  data_dir_default: boolean
  /** Setup made this folder and nothing has been downloaded or built into it yet. */
  can_move_data: boolean
}

export function setupPc(secret: string, signal?: AbortSignal): Promise<SetupPc> {
  return api('/api/setup/pc', { ...post({ secret }), signal })
}

/** The add-on list, as Settings › Add-ons gets it. `refresh`: look afresh (Check again). */
export function setupAddons(secret: string, refresh = false): Promise<AddonsResponse> {
  return api('/api/setup/addons', post(refresh ? { secret, refresh } : { secret }))
}

/** Start at login is only chosen here; it is turned on once the passkey exists. */
export function setupStartup(secret: string, enabled: boolean): Promise<{ enabled: boolean }> {
  return api('/api/setup/startup', post({ secret, enabled }))
}

/** Move the data folder: 202, then setup's server starts again in the new folder. */
export function setupDataFolder(secret: string, path: string): Promise<{ ok: true }> {
  return api('/api/setup/data-folder', post({ secret, path }))
}

/** The model screens' calls behind the setup secret (no passkey: there is none yet). */
export function setupModelsApi(secret: string): ModelsApi {
  return {
    getCatalog: () => api<Catalog>('/api/setup/catalog', post({ secret })),
    async startInstall(items, makeDefault) {
      return (await api<{ job: Job }>('/api/setup/install', post({ secret, items, make_default: makeDefault }))).job
    },
    async getJob() {
      return (await api<{ job: Job | null }>('/api/setup/install/status', post({ secret }))).job
    },
    async cancel() {
      return (await api<{ job: Job | null }>('/api/setup/install/cancel', post({ secret }))).job
    },
    async retry() {
      return (await api<{ job: Job }>('/api/setup/install/retry', post({ secret }))).job
    },
    switchLocal: (card) => api<ChatSwitch>('/api/setup/chat', post({ secret, source: 'local', card })),
    switchExternal: (baseUrl, apiKey, model) =>
      api<ChatSwitch>('/api/setup/chat', post({ secret, source: 'external', base_url: baseUrl, api_key: apiKey, model })),
    passkey: false,
  }
}

/** Building the analysis sandbox behind the setup secret. */
export function setupSandboxApi(secret: string): SandboxBuildApi {
  return {
    async getBuild() {
      return (await api<{ build: SandboxBuildState }>('/api/setup/sandbox/status', post({ secret }))).build
    },
    async startBuild() {
      return (await api<{ build: SandboxBuildState }>('/api/setup/sandbox/build', post({ secret }))).build
    },
  }
}

function samePath(a: string, b: string): boolean {
  const norm = (p: string) => p.trim().replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
  return norm(a) === norm(b)
}

/** After a data-folder move: true once `/api/health` answers again from the server in the new
 *  folder (it was seen to stop, or it already names `target`). The caller then checks the
 *  setup link again with the secret it holds. */
export function waitForMove(secret: string, target: string, options: WaitOptions = {}): Promise<boolean> {
  let stopped = false
  return waitUntil(
    async (signal) => {
      try {
        await api('/api/health', { signal })
      } catch (e) {
        stopped = true
        throw e
      }
      if (stopped) return true
      return samePath((await setupPc(secret, signal)).data_dir, target)
    },
    { timeoutMs: 120_000, ...options },
  )
}
