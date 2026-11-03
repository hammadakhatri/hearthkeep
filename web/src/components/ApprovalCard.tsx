import { useId, useState } from 'react'
import { api, isApiError, post } from '../api/client'
import type { ApprovalStatus } from '../api/events'
import { passkeyErrorMessage, stepUp } from '../api/webauthn'
import { AlertIcon, PasskeyIcon } from '../shell/icons'
import { DiffView } from './DiffView'
import type { ApprovalRequest } from '../views/chatModel'

export type { ApprovalRequest }

type Resolved = Exclude<ApprovalStatus, 'pending'>
type Outcome = Resolved | 'handled' | 'gone'

const OUTCOME_TEXT: Record<Outcome, string> = {
  approved: 'Approved',
  declined: 'Declined',
  expired: 'This request expired',
  handled: 'Already handled',
  gone: 'This request is no longer available',
}

/** What the tool will do, as readable text: the arguments when there are any. */
function detailText(detail: unknown): string {
  const args =
    detail && typeof detail === 'object' && 'arguments' in detail ? (detail as { arguments: unknown }).arguments : detail
  if (typeof args === 'string') return args
  try {
    return JSON.stringify(args, null, 2) ?? ''
  } catch {
    return String(args)
  }
}

type Fact = { label: string; value: string | string[]; small?: boolean }

/** The GitHub detail fields worth a line each, in reading order. */
const FACTS: [key: string, label: string][] = [
  ['repo', 'Repo'],
  ['branch', 'Branch'],
  ['head', 'From'],
  ['base', 'Into'],
  ['kind', 'Kind'],
  ['number', 'Number'],
  ['method', 'Method'],
  ['path', 'Path'],
  ['title', 'Title'],
  ['message', 'Message'],
  ['body', 'Body'],
  ['command', 'Command'],
  ['files', 'Files'],
  ['commits', 'Commits'],
  ['network', 'Network'],
  ['head_sha', 'Head'],
  ['lock_hash', 'Lock'],
]
const TECHNICAL = new Set(['head_sha', 'lock_hash'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function factsOf(detail: Record<string, unknown>): Fact[] {
  const facts: Fact[] = []
  for (const [key, label] of FACTS) {
    const value = detail[key]
    if (typeof value === 'string' && value) {
      facts.push({ label, value: TECHNICAL.has(key) ? value.slice(0, 7) : value, small: TECHNICAL.has(key) })
    } else if (typeof value === 'number') {
      facts.push({ label, value: `#${value}` })
    } else if (typeof value === 'boolean') {
      if (key === 'network') facts.push({ label, value: value ? 'Needs the internet' : 'Stays on this computer' })
    } else if (Array.isArray(value)) {
      const items = value.filter((v): v is string => typeof v === 'string')
      if (items.length) facts.push({ label, value: items })
    }
  }
  return facts
}

/** A detail with a unified diff (GitHub tools): the facts as a short list, then the diff. */
function DiffDetail({ detail, diff }: { detail: Record<string, unknown>; diff: string }) {
  const facts = factsOf(detail)
  return (
    <div className="approval-diff" data-testid="approval-diff">
      {facts.length > 0 && (
        <dl className="approval-facts">
          {facts.map((fact) => (
            <div key={fact.label} className={`approval-fact${fact.small ? ' small' : ''}`}>
              <dt>{fact.label}</dt>
              <dd>
                {Array.isArray(fact.value) ? (
                  <ul>
                    {fact.value.map((item, i) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                ) : (
                  fact.value
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {diff.trim() ? <DiffView diff={diff} alwaysOpen /> : <p className="diff-note approval-nodiff">No changes to show.</p>}
    </div>
  )
}

type Props = {
  request: ApprovalRequest
  /** The status the server last reported (from the turn's events). */
  status: ApprovalStatus
  onResolved?: (status: Resolved) => void
}

/**
 * An action waiting for the owner. Approve (when the action leaves the computer, only after
 * Face ID / Touch ID) or Decline, which never needs a passkey.
 */
export function ApprovalCard({ request, status, onResolved }: Props) {
  const titleId = useId()
  const [busy, setBusy] = useState<'approve' | 'decline' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const shown: Outcome | null = status !== 'pending' ? status : outcome
  const record = isRecord(request.detail) ? request.detail : null
  const diff = record && typeof record.diff === 'string' ? record.diff : null
  const note = record && typeof record.note === 'string' && record.note ? record.note : null
  const detail = diff === null ? detailText(request.detail) : ''

  async function resolve(approve: boolean) {
    setBusy(approve ? 'approve' : 'decline')
    setError(null)
    const path = `/api/approvals/${encodeURIComponent(request.approval_id)}`
    try {
      let token = approve && request.requires_step_up ? await stepUp('approval', request.approval_id) : undefined
      let result: { status: Resolved }
      try {
        result = await api<{ status: Resolved }>(path, post(token ? { approve, step_up_token: token } : { approve }))
      } catch (e) {
        // The confirmation was too old (or the server wants one after all): ask once more.
        if (!(approve && isApiError(e, 'step_up_required'))) throw e
        token = await stepUp('approval', request.approval_id)
        result = await api<{ status: Resolved }>(path, post({ approve, step_up_token: token }))
      }
      setOutcome(result.status)
      onResolved?.(result.status)
    } catch (e) {
      if (isApiError(e, 'already_resolved')) setOutcome('handled')
      else if (isApiError(e, 'expired')) setOutcome('expired')
      else if (isApiError(e, 'not_found')) setOutcome('gone')
      else if (isApiError(e, 'step_up_required')) setError("Face ID / Touch ID couldn't confirm it. Try again.")
      else if (isApiError(e, 'network')) setError("Can't reach Hearthkeep. Try again in a moment.")
      else if (isApiError(e)) setError('Something went wrong. Try again.')
      else setError(passkeyErrorMessage(e, 'Approval'))
    }
    setBusy(null)
  }

  return (
    <div className="approval" role="group" aria-labelledby={titleId} data-status={shown ?? 'pending'}>
      <div className="approval-hd">
        <span className="approval-kicker">
          <span className="approval-dot" aria-hidden="true" />
          {shown ? 'Your decision' : 'Needs your approval'}
        </span>
        <b id={titleId}>{request.summary}</b>
        <span className="approval-tool">{request.tool}</span>
      </div>
      {note && (
        <div className="approval-warning" role="note">
          <AlertIcon />
          <span>{note}</span>
        </div>
      )}
      {record && diff !== null && <DiffDetail detail={record} diff={diff} />}
      {detail && (
        <pre className="approval-detail" data-testid="approval-detail">
          <code>{detail}</code>
        </pre>
      )}
      <div className="approval-ft" aria-live="polite">
        {shown ? (
          <span className="approval-outcome" data-outcome={shown}>
            {OUTCOME_TEXT[shown]}
          </span>
        ) : (
          <>
            <button
              type="button"
              className="btn primary"
              onClick={() => resolve(true)}
              disabled={busy !== null}
              aria-busy={busy === 'approve'}
            >
              {request.requires_step_up && <PasskeyIcon />}
              Approve
            </button>
            <button
              type="button"
              className="btn ghost"
              onClick={() => resolve(false)}
              disabled={busy !== null}
              aria-busy={busy === 'decline'}
            >
              Decline
            </button>
            {request.requires_step_up && <span className="approval-note">Approving asks for Face ID / Touch ID</span>}
          </>
        )}
      </div>
      {error && !shown && (
        <div className="note error approval-error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </div>
      )}
    </div>
  )
}
