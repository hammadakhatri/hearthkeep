/** Shapes of GET /api/models, and the model catalog, downloads and the chat model choice. */

import { api, post } from './client'
import { stepUp } from './webauthn'

export type ModelInfo = {
  id: string
  name: string
  kind: 'chat' | 'image'
  capabilities: string[]
  vram_gb: number
  source: 'shipped' | 'owner'
  resident: boolean
  managed: boolean
}

export type CardError = { id_or_file: string; message: string }

export type GpuStatus = {
  total_gb: number
  used_gb: number
  resident: string[]
  waiting: number
}

export type ModelsResponse = {
  models: ModelInfo[]
  errors: CardError[]
  gpu: GpuStatus
}

// ---- the download catalog, install jobs and choosing the chat model -------------------------

export type PcInfo = {
  gpu_name: string | null
  vram_gb: number | null
  nvidia: boolean
  ram_gb: number | null
  free_disk_gb: number
}

export type CatalogKind = 'runtime' | 'chat' | 'comfyui' | 'image'

export type CatalogItem = {
  id: string
  kind: CatalogKind
  name: string
  note: string
  licence: string
  licence_url: string
  size_bytes: number
  needs_nvidia: boolean
  variant: '' | 'cuda' | 'vulkan' | 'cpu'
  /** The model card a chat or image item installs (empty for the engines). */
  card: string
  installed: boolean
  fits: boolean
}

export type Recommendation = { chat: string; runtime: string; reason: 'fits_gpu' | 'no_nvidia' | 'small_gpu' }

/** Where chat comes from once Hearthkeep (re)starts. `model` is a card id when local. */
export type ChatState = { source: 'local' | 'external' | 'none'; model: string | null; base_url: string | null }

export type Catalog = {
  pc: PcInfo
  recommended: Recommendation | null
  items: CatalogItem[]
  chat: ChatState
  /** A chat engine is there (a catalog build, or one set up by hand): a chat model download
   *  adds none. The installer's own check. */
  runtime_ready: boolean
  restart_needed: boolean
  supervised: boolean
}

export type JobItemState = 'queued' | 'downloading' | 'verifying' | 'unpacking' | 'done' | 'failed' | 'cancelled'

export type JobItem = {
  id: string
  name: string
  state: JobItemState
  done_bytes: number
  total_bytes: number
  speed_bps: number
  /** network, not_found, too_big, checksum, no_space, cancelled, unpack or config_not_updated. */
  error: string | null
}

export type Job = {
  id: string
  state: 'running' | 'done' | 'failed' | 'cancelled'
  items: JobItem[]
  restart_needed: boolean
}

export type ChatSwitch = ChatState & { restart_needed: boolean }

/** Bytes in decimal units, as download sizes are usually given: "3.0 GB", "287 MB". */
export function formatBytes(bytes: number): string {
  const n = Math.max(0, bytes)
  if (n < 1000) return `${Math.round(n)} B`
  if (n < 1e6) return `${Math.round(n / 1e3)} KB`
  if (n < 1e9) return `${Math.round(n / 1e6)} MB`
  return `${(n / 1e9).toFixed(1)} GB`
}

export function getCatalog(): Promise<Catalog> {
  return api<Catalog>('/api/models/catalog')
}

/** Asks for the passkey (purpose `download`), then starts the job. Names catalog item ids only. */
export async function startInstall(items: string[], makeDefault: boolean): Promise<Job> {
  const token = await stepUp('download')
  const result = await api<{ job: Job }>(
    '/api/models/install',
    post({ items, make_default: makeDefault, step_up_token: token }),
  )
  return result.job
}

/** The current (or last) job, or null when there has been none since Hearthkeep started. */
export async function getJob(): Promise<Job | null> {
  return (await api<{ job: Job | null }>('/api/models/install')).job
}

export async function cancelInstall(): Promise<Job | null> {
  return (await api<{ job: Job | null }>('/api/models/install/cancel', { method: 'POST' })).job
}

/** Runs the failed and stopped items again; they were approved when the job started. */
export async function retryInstall(): Promise<Job> {
  return (await api<{ job: Job }>('/api/models/install/retry', { method: 'POST' })).job
}

/** Chat from an installed model on this PC. Needs only the session. */
export function switchToLocalChat(card: string): Promise<ChatSwitch> {
  return api<ChatSwitch>('/api/models/chat', post({ source: 'local', card }))
}

/** Chat from a model server the owner runs. Asks for the passkey (purpose `settings`). A null
 *  key keeps the stored one (when chat already uses a server the owner runs); an empty one
 *  removes it. A null model keeps the saved model name. */
export async function switchToExternalChat(
  baseUrl: string,
  apiKey: string | null,
  model: string | null = null,
): Promise<ChatSwitch> {
  const token = await stepUp('settings')
  return api<ChatSwitch>(
    '/api/models/chat',
    post({ source: 'external', base_url: baseUrl, api_key: apiKey, model, step_up_token: token }),
  )
}
