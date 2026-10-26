/** The live event stream of one agent turn (Server-Sent Events). */

export type ApprovalStatus = 'pending' | 'approved' | 'declined' | 'expired'

export type AgentEvent = { seq: number } & (
  | { type: 'text_delta'; text: string }
  | { type: 'reasoning_delta'; text: string }
  | { type: 'step_started'; step_id: string; tool: string; summary: string }
  | { type: 'step_finished'; step_id: string; ok: boolean; preview: string }
  | {
      type: 'approval_required'
      approval_id: string
      tool: string
      summary: string
      detail: unknown
      requires_step_up: boolean
    }
  | { type: 'approval_resolved'; approval_id: string; status: Exclude<ApprovalStatus, 'pending'> }
  | { type: 'turn_done' }
  | { type: 'turn_error'; message: string }
)

export type AgentEventType = AgentEvent['type']

export const EVENT_TYPES: readonly AgentEventType[] = [
  'text_delta',
  'reasoning_delta',
  'step_started',
  'step_finished',
  'approval_required',
  'approval_resolved',
  'turn_done',
  'turn_error',
]

const TERMINAL: ReadonlySet<AgentEventType> = new Set(['turn_done', 'turn_error'])

/** Waits before reconnecting after a drop; the last one repeats. */
const RETRY_MS = [500, 1000, 2000, 5000]

/**
 * Follow a turn's events. Each event reaches `onEvent` exactly once, in order: after a drop
 * the stream reopens from the last seen `seq`, and anything at or below it is ignored. Closes
 * itself after `turn_done` or `turn_error`. Returns a function that stops listening.
 */
export function subscribeTurn(
  chatId: string,
  turnId: string,
  onEvent: (e: AgentEvent) => void,
  afterSeq = 0,
): () => void {
  let lastSeq = afterSeq
  let source: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let failures = 0
  let stopped = false

  const stop = () => {
    stopped = true
    clearTimeout(timer)
    source?.close()
    source = null
  }

  const open = () => {
    const url = `/api/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(turnId)}/events?after=${lastSeq}`
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
      const wait = RETRY_MS[Math.min(failures, RETRY_MS.length - 1)]
      failures += 1
      timer = setTimeout(() => {
        if (!stopped) open()
      }, wait)
    }
  }

  open()
  return stop
}
