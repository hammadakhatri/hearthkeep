/** The live event stream of one agent turn (Server-Sent Events). */

import { api, isApiError } from './client'
import type { NumberMark } from './runs'

export type ApprovalStatus = 'pending' | 'approved' | 'declined' | 'expired'

export type AgentEvent = { seq: number } & (
  | { type: 'text_delta'; text: string }
  | { type: 'reasoning_delta'; text: string }
  | { type: 'step_started'; step_id: string; tool: string; summary: string }
  | { type: 'step_finished'; step_id: string; ok: boolean; preview: string }
  /** What a step made for the owner to see (generated images, analysis runs, files), sent just before it finishes. */
  | { type: 'attachments'; step_id: string; items: Attachment[] }
  | {
      type: 'approval_required'
      approval_id: string
      tool: string
      summary: string
      detail: unknown
      requires_step_up: boolean
    }
  | { type: 'approval_resolved'; approval_id: string; status: Exclude<ApprovalStatus, 'pending'> }
  /** The server's check of the finished reply's numbers, sent just before `turn_done`. */
  | { type: 'number_check'; text: string; marks: NumberMark[] }
  | { type: 'turn_done' }
  | { type: 'turn_error'; message: string }
)

export type Attachment = { kind: 'image' | 'run' | 'file'; id: string }

export type AgentEventType = AgentEvent['type']

export const EVENT_TYPES: readonly AgentEventType[] = [
  'text_delta',
  'reasoning_delta',
  'step_started',
  'step_finished',
  'attachments',
  'approval_required',
  'approval_resolved',
  'number_check',
  'turn_done',
  'turn_error',
]

const TERMINAL: ReadonlySet<AgentEventType> = new Set(['turn_done', 'turn_error'])

/** Waits before reconnecting after a drop; the last one repeats. */
const RETRY_MS = [500, 1000, 2000, 5000]

/** Consecutive drops with no new event before giving up on the stream. */
export const MAX_FAILURES = 5

/**
 * Why a stream gave up: the session ended (the sign-in screen is already on its way), the chat
 * is gone, or the server stayed unreachable.
 */
export type StreamFailure = 'unauthenticated' | 'not_found' | 'unreachable'

/**
 * Follow a turn's events. Each event reaches `onEvent` exactly once, in order: after a drop
 * the stream reopens from the last seen `seq`, and anything at or below it is ignored. Closes
 * itself after `turn_done` or `turn_error`. After a drop it checks the chat once with a plain
 * request: a 401 or 404 ends the stream at once, and so do MAX_FAILURES drops in a row; then
 * `onFail` says why. Returns a function that stops listening.
 */
export function subscribeTurn(
  chatId: string,
  turnId: string,
  onEvent: (e: AgentEvent) => void,
  afterSeq = 0,
  onFail?: (reason: StreamFailure) => void,
): () => void {
  let lastSeq = afterSeq
  let source: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let failures = 0
  let stopped = false
  const chatPath = `/api/chats/${encodeURIComponent(chatId)}`

  const stop = () => {
    stopped = true
    clearTimeout(timer)
    source?.close()
    source = null
  }

  const fail = (reason: StreamFailure) => {
    if (stopped) return
    stop()
    onFail?.(reason)
  }

  /** An EventSource error hides the HTTP status, so ask the chat itself what is wrong. */
  const probe = async (): Promise<StreamFailure | null> => {
    try {
      await api(`${chatPath}/messages`)
      return null
    } catch (error) {
      // api() has already announced an expired session (UNAUTHENTICATED_EVENT) for a 401.
      if (isApiError(error) && error.status === 401) return 'unauthenticated'
      if (isApiError(error) && error.status === 404) return 'not_found'
      return null // unreachable or failing: worth another try
    }
  }

  const open = () => {
    const url = `${chatPath}/turns/${encodeURIComponent(turnId)}/events?after=${lastSeq}`
    const es = new EventSource(url)
    source = es

    const handle = (type: AgentEventType) => (message: MessageEvent) => {
      if (stopped || es !== source) return
      const seq = Number(message.lastEventId)
      if (!Number.isFinite(seq) || seq <= lastSeq) return
      let data: Record<string, unknown>
      try {
        data = JSON.parse(message.data)
      } catch {
        return
      }
      lastSeq = seq
      failures = 0
      onEvent({ ...data, type, seq } as AgentEvent)
      if (TERMINAL.has(type)) stop()
    }
    for (const type of EVENT_TYPES) es.addEventListener(type, handle(type) as EventListener)

    // Reconnect ourselves (rather than the browser's own retry) so the next request carries
    // the last seq we actually handled, and backs off while the server is away.
    es.onerror = () => {
      if (stopped || es !== source) return
      es.close()
      source = null
      failures += 1
      if (failures >= MAX_FAILURES) {
        fail('unreachable')
        return
      }
      const wait = RETRY_MS[Math.min(failures - 1, RETRY_MS.length - 1)]
      void probe().then((reason) => {
        if (stopped) return
        if (reason) {
          fail(reason)
          return
        }
        timer = setTimeout(() => {
          if (!stopped) open()
        }, wait)
      })
    }
  }

  open()
  return stop
}
