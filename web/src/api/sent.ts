/** Sending things to the owner's devices, and this device's "Sent to you" tray. */

import { api, post } from './client'
import type { NumberMark } from './runs'

export type SentKind = 'chart' | 'table' | 'file' | 'summary'

/** One thing sent to this device. `text`/`marks` are a summary's; `chart` is a chart's spec. */
export type SentItemInfo = {
  id: string
  kind: SentKind
  title: string
  text: string | null
  marks: NumberMark[] | null
  chart: Record<string, unknown> | null
  created_at: string
  dismissed: boolean
}

export type DeviceInfo = { id: string; device_name: string; created_at: string; current: boolean }

/**
 * Send a chart, table or file (by its id) or a summary (by its assistant message id: the
 * server takes the text itself) to all the owner's devices or to the listed device ids.
 */
export async function sendItem(body: {
  kind: SentKind
  ref_id: string
  devices: 'all' | string[]
}): Promise<void> {
  await api('/api/sent', post(body))
}

export function listSent(): Promise<{ items: SentItemInfo[]; unread: number }> {
  return api('/api/sent')
}

export async function sentCount(): Promise<number> {
  return (await api<{ unread: number }>('/api/sent/count')).unread
}

export async function dismissSent(id: string): Promise<void> {
  await api(`/api/sent/${encodeURIComponent(id)}/dismiss`, { method: 'POST' })
}

/** A table as CSV, a file's bytes or a summary as text. Built from the item's id only. */
export function sentDownloadUrl(id: string): string {
  return `/api/sent/${encodeURIComponent(id)}/download`
}

export function listDevices(): Promise<DeviceInfo[]> {
  return api('/api/devices')
}
