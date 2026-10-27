/** What the chat thread shows, built from saved messages and live turn events. */

import type { AgentEvent, ApprovalStatus, Attachment } from '../api/events'
import type { NumberCheck } from '../api/runs'

export type ChatSummary = {
  id: string
  title: string
  think: boolean
  created_at: string
  active_turn: string | null
}

export type ServerMessage = {
  id: number
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  tool_calls: { id?: string; function?: { name?: string } }[] | null
  tool_call_id: string | null
  created_at: string
  /** On a tool message: ids of the images that tool call made, in order. */
  images?: string[]
  /** On a tool message: ids of the analysis runs that tool call made, in order. */
  runs?: string[]
  /** On an assistant message: the turn's number check (only the last message of a checked turn). */
  number_check?: NumberCheck | null
}

export type Step = {
  id: string
  tool: string
  summary: string
  state: 'running' | 'ok' | 'failed'
  /** Ids of the images this step made, in order. */
  images?: string[]
  /** Ids of the analysis runs this step made, in order. */
  runs?: string[]
  /** Ids of the files this step brought into the chat or made, in order. */
  files?: string[]
}

export type ApprovalRequest = {
  approval_id: string
  tool: string
  summary: string
  detail: unknown
  requires_step_up: boolean
}

export type UserItem = { kind: 'user'; key: string; text: string }

export type AssistantItem = {
  kind: 'assistant'
  key: string
  turnId: string | null
  text: string
  reasoning: string
  steps: Step[]
  approvals: { request: ApprovalRequest; status: ApprovalStatus }[]
  state: 'streaming' | 'done' | 'error'
  error: string | null
  /** When the turn started (ms since epoch) and how long its tool steps took. */
  startedAt: number
  workedMs: number
  /** Text written before the latest tool step: the next text starts a new paragraph. */
  pendingBreak: boolean
  /** The server's check of the reply's numbers, once the reply is finished. */
  check?: NumberCheck | null
  /** The id of the turn's final saved reply: what "Send" names for a summary. */
  messageId?: number
}

export type Item = UserItem | AssistantItem

export function liveTurn(turnId: string, startedAt: number): AssistantItem {
  return {
    kind: 'assistant',
    key: `turn-${turnId}`,
    turnId,
    text: '',
    reasoning: '',
    steps: [],
    approvals: [],
    state: 'streaming',
    error: null,
    startedAt,
    workedMs: 0,
    pendingBreak: false,
  }
}

/** Fold one event into the turn it belongs to. Pure: returns a new item. */
export function applyEvent(item: AssistantItem, e: AgentEvent, now: number): AssistantItem {
  switch (e.type) {
    case 'text_delta': {
      const join = item.pendingBreak && item.text && e.text.trim() ? '\n\n' : ''
      return { ...item, text: item.text + join + e.text, pendingBreak: join ? false : item.pendingBreak }
    }
    case 'reasoning_delta':
      return { ...item, reasoning: item.reasoning + e.text }
    case 'step_started':
      return {
        ...item,
        pendingBreak: item.pendingBreak || item.text.trim() !== '',
        steps: [...item.steps, { id: e.step_id, tool: e.tool, summary: e.summary, state: 'running' }],
      }
    case 'step_finished':
      return {
        ...item,
        workedMs: worked(item, now),
        steps: item.steps.map((s) => (s.id === e.step_id ? { ...s, state: e.ok ? 'ok' : 'failed' } : s)),
      }
    case 'attachments': {
      const idsOf = (kind: Attachment['kind']) =>
        e.items.filter((a) => a.kind === kind && typeof a.id === 'string').map((a) => a.id)
      const merge = (had: string[] | undefined, more: string[]) => [...new Set([...(had ?? []), ...more])]
      const [images, runs, files] = [idsOf('image'), idsOf('run'), idsOf('file')]
      return {
        ...item,
        steps: item.steps.map((s) => {
          if (s.id !== e.step_id) return s
          const next = { ...s, images: merge(s.images, images) }
          if (runs.length) next.runs = merge(s.runs, runs)
          if (files.length) next.files = merge(s.files, files)
          return next
        }),
      }
    }
    case 'approval_required': {
      if (item.approvals.some((a) => a.request.approval_id === e.approval_id)) return item
      const request: ApprovalRequest = {
        approval_id: e.approval_id,
        tool: e.tool,
        summary: e.summary,
        detail: e.detail,
        requires_step_up: e.requires_step_up,
      }
      return { ...item, approvals: [...item.approvals, { request, status: 'pending' }] }
    }
    case 'approval_resolved':
      return setApproval(item, e.approval_id, e.status)
    case 'number_check':
      return { ...item, check: { text: e.text, marks: e.marks } }
    case 'turn_done':
      return { ...item, state: 'done', workedMs: worked(item, now), approvals: expireOpen(item) }
    case 'turn_error':
      return { ...item, state: 'error', error: e.message, workedMs: worked(item, now), approvals: expireOpen(item) }
  }
}

/** Every image the turn made so far, in the order its steps made them. */
export function turnImages(item: AssistantItem): string[] {
  return item.steps.flatMap((s) => s.images ?? [])
}

/** Time from the start of the turn to now, once it has used a tool. */
function worked(item: AssistantItem, now: number): number {
  return item.steps.length ? Math.max(0, now - item.startedAt) : item.workedMs
}

function expireOpen(item: AssistantItem) {
  return item.approvals.map((a) => (a.status === 'pending' ? { ...a, status: 'expired' as const } : a))
}

export function setApproval(item: AssistantItem, approvalId: string, status: ApprovalStatus): AssistantItem {
  return {
    ...item,
    approvals: item.approvals.map((a) => (a.request.approval_id === approvalId ? { ...a, status } : a)),
  }
}

function toolFailed(content: string): boolean {
  try {
    const parsed = JSON.parse(content)
    return parsed !== null && typeof parsed === 'object' && 'error' in parsed
  } catch {
    return false
  }
}

function time(iso: string): number {
  const t = Date.parse(iso)
  return Number.isNaN(t) ? 0 : t
}

/** Saved messages -> thread items. Each run of assistant/tool messages after a user message is one turn. */
export function itemsFromMessages(messages: ServerMessage[]): Item[] {
  const items: Item[] = []
  let turn: AssistantItem | null = null
  let userAt = 0

  for (const m of messages) {
    if (m.role === 'user') {
      turn = null
      userAt = time(m.created_at)
      items.push({ kind: 'user', key: `m${m.id}`, text: m.content })
      continue
    }
    if (m.role !== 'assistant' && m.role !== 'tool') continue
    if (turn === null) {
      turn = { ...liveTurn('', userAt || time(m.created_at)), key: `m${m.id}`, turnId: null, state: 'done' }
      items.push(turn)
    }
    let next: AssistantItem = turn
    if (m.role === 'assistant') {
      if (m.content.trim()) next = applyEvent(next, { type: 'text_delta', seq: 0, text: m.content }, 0)
      if (m.number_check) next = { ...next, check: m.number_check }
      if (m.content.trim()) next = { ...next, messageId: m.id }
      for (const call of m.tool_calls ?? []) {
        const tool = call.function?.name || 'a tool'
        const step_id = call.id ?? `${m.id}-${next.steps.length}`
        next = applyEvent(next, { type: 'step_started', seq: 0, step_id, tool, summary: `Used ${tool}` }, 0)
      }
    } else {
      const state = toolFailed(m.content) ? 'failed' : 'ok'
      next = { ...next, steps: next.steps.map((s) => (s.id === m.tool_call_id ? { ...s, state } : s)) }
      if (m.tool_call_id && (m.images?.length || m.runs?.length)) {
        // The same shape a live turn's attachments event has, so it shows the same way.
        const items: Attachment[] = [
          ...(m.images ?? []).map((id) => ({ kind: 'image' as const, id })),
          ...(m.runs ?? []).map((id) => ({ kind: 'run' as const, id })),
        ]
        next = applyEvent(next, { type: 'attachments', seq: 0, step_id: m.tool_call_id, items }, 0)
      }
    }
    // A turn that used tools worked from the owner's message until its last saved message.
    if (next.steps.length) next = { ...next, workedMs: Math.max(0, time(m.created_at) - next.startedAt) }
    turn = next
    items[items.length - 1] = next
  }
  return items
}

/** "Worked for 14 seconds · 4 steps" */
export function workedLabel(ms: number, steps: number): string {
  const seconds = Math.max(1, Math.round(ms / 1000))
  const span =
    seconds < 90 ? `${seconds} ${seconds === 1 ? 'second' : 'seconds'}` : `${Math.round(seconds / 60)} minutes`
  return `Worked for ${span} · ${steps} ${steps === 1 ? 'step' : 'steps'}`
}
