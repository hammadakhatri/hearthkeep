/** The Analysis gallery: files and charts grouped by chat, and one chart with its headline. */

import { api } from './client'
import type { ChatFileInfo } from './files'
import type { NumberCheck } from './runs'

export type AnalysisChart = { id: string; run_id: string; title: string; created_at: string }

export type AnalysisChat = {
  chat_id: string
  title: string
  /** The time of the chat's newest file or run: also the cursor for the next page. */
  updated_at: string
  files: ChatFileInfo[]
  charts: AnalysisChart[]
}

/** `more`: the server has another page after this one. */
export const PAGE_SIZE = 20

/** A page of chats, newest first; `before` is the last `updated_at` already shown. */
export function getAnalysis(before?: string): Promise<{ chats: AnalysisChat[]; more: boolean }> {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) })
  if (before) query.set('before', before)
  return api(`/api/analysis?${query}`)
}

/** `GET /api/outputs/{id}` for a chart: its spec, and the reply that went with it. */
export type ChartOutput = {
  id: string
  run_id: string
  chat_id: string
  kind: 'chart'
  name: string
  spec: Record<string, unknown>
  turn_id: string | null
  headline: Pick<NumberCheck, 'text' | 'marks'> | null
}

export function getChartOutput(id: string): Promise<ChartOutput> {
  return api<ChartOutput>(`/api/outputs/${encodeURIComponent(id)}`)
}
