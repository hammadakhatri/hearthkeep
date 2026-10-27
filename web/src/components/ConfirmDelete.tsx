import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { isApiError } from '../api/client'
import { passkeyErrorMessage } from '../api/webauthn'
import { AlertIcon } from '../shell/icons'

/** A plain sentence for anything a deletion can throw (passkey or server). */
export function deleteErrorMessage(error: unknown): string {
  if (isApiError(error, 'turn_in_progress')) {
    return 'Hearthkeep is still answering in this chat. Wait for it to finish, then try again.'
  }
  if (isApiError(error, 'step_up_required')) return "Your passkey couldn't be confirmed. Try again."
  return passkeyErrorMessage(error, 'Delete')
}

/**
 * Asks before deleting something for good. "Delete with passkey" runs `onConfirm` (which does the
 * passkey step-up and the deletion); while it runs both buttons wait, and if it throws the dialog
 * stays and says why. Focus is kept inside, Escape cancels, and focus goes back to whatever opened
 * it when it closes.
 */
export function ConfirmDelete({
  title,
  body,
  onConfirm,
  onCancel,
}: {
  title: string
  body: string
  onConfirm: () => Promise<void>
  onCancel: () => void
}) {
  const titleId = useId()
  const bodyId = useId()
  const dialog = useRef<HTMLDivElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const opener = document.activeElement as HTMLElement | null
    // The safe choice first: Enter on open never deletes anything.
    cancel.current?.focus()
    return () => {
      alive.current = false
      if (opener && opener.isConnected) opener.focus()
    }
  }, [])

  async function confirm() {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm()
    } catch (err) {
      if (alive.current) setError(deleteErrorMessage(err))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation()
      if (!busy) onCancel()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>('button') ?? [])]
    if (!focusable.length) {
      event.preventDefault()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const at = document.activeElement
    if (event.shiftKey && (at === first || !dialog.current?.contains(at))) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (at === last || !dialog.current?.contains(at))) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="scrim confirm-scrim" onClick={() => !busy && onCancel()}>
      <div
        ref={dialog}
        className="confirm"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        aria-busy={busy || undefined}
        onKeyDown={onKeyDown}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id={titleId} className="confirm-title">
          <bdi>{title}</bdi>
        </h2>
        <p id={bodyId} className="confirm-body">
          {body}
        </p>
        {error && (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>{error}</span>
          </div>
        )}
        <div className="confirm-actions">
          {/* aria-disabled, not disabled: a disabled button would drop the focus out of the dialog. */}
          <button
            ref={cancel}
            type="button"
            className="btn"
            aria-disabled={busy || undefined}
            onClick={() => !busy && onCancel()}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn danger"
            aria-disabled={busy || undefined}
            onClick={() => void confirm()}
          >
            Delete with passkey
          </button>
        </div>
      </div>
    </div>
  )
}
