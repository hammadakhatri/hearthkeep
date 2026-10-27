/** Saved analysis runs: what a run did and made, and where to download its outputs. */

import { api } from './client'

/** A number in a reply, as the server's check found it. `start`/`end` are UTF-16 offsets. */
export type NumberMark = {
  start: number
  end: number
  text: string
  kind: 'computed' | 'document' | 'unverified'
  page: number | null
  note: string | null
}

/** The server's check of one finished reply. Marks apply only when `text` is the shown text. */
export type NumberCheck = { text: string; marks: NumberMark[] }

export type RunValue = {
  name: string
  value: number | string | null
  unit: string | null
  note: string | null
  is_date: boolean
  kind: 'computed' | 'document'
  page: number | null
}

export type RunChart = { id: string; title: string; spec: Record<string, unknown> }

export type RunTable = {
  id: string
  name: string
  columns: string[]
  rows: unknown[][]
  /** The file's own row numbers; empty when the table was reshaped. */
  row_numbers: number[]
  note: string | null
  total_rows: number
}

export type RunFile = { id: string; name: string; mime: string; size: number }

/** `GET /api/runs/{id}`. */
export type RunDetail = {
  id: string
  chat_id: string
  turn_id: string
  purpose: 'analysis' | 'vision'
  status: string
  error: string | null
  duration_ms: number
  /** The Python that ran; null for a vision run. */
  script: string | null
  stdout: string
  stdout_truncated: boolean
  values: RunValue[]
  cleaning: string[]
  sections: string[]
  charts: RunChart[]
  tables: RunTable[]
  files: RunFile[]
  created_at: string
}

export function getRun(id: string): Promise<RunDetail> {
  return api<RunDetail>(`/api/runs/${encodeURIComponent(id)}`)
}

/** A table as CSV, or an output file's bytes. */
export function outputDownloadUrl(id: string): string {
  return `/api/outputs/${encodeURIComponent(id)}/download`
}
