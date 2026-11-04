import { useId } from 'react'
import { isApiError } from '../api/client'
import { AlertIcon } from '../shell/icons'

/** When the choice couldn't be saved and the server gave no sentence of its own; Settings and
 *  setup say the same. */
export const STARTUP_FAILED = "Start at login couldn't be changed. Try again."

/** When setup couldn't read start at login: the switch shows "Not known" and can't be used. */
export const STARTUP_UNKNOWN = "Couldn't check start at login. You can set it in Settings after setup."

/** The sentence for a failed change: the server's own `message` for `startup_failed` (it says what
 *  Windows may be blocking), else `STARTUP_FAILED`. Other errors' messages are never shown here. */
export function startupFailedMessage(error: unknown): string {
  const message = isApiError(error, 'startup_failed') ? (error.body as { message?: unknown } | null)?.message : undefined
  return typeof message === 'string' && message ? message : STARTUP_FAILED
}

/** What the switch shows; the same shape `GET /api/settings/startup` answers. */
export type StartupState = {
  supported: boolean
  enabled: boolean
  /** On, but the task starts Hearthkeep with another data folder. */
  other_data_folder?: boolean
}

type Props = {
  /** `null` while it is still being read. */
  state: StartupState | null
  /** The owner asked for this value; the caller does the work (Settings: a passkey and the server; setup: a choice). */
  onChange: (next: boolean) => void
  busy?: boolean
  error?: string | null
  /** It couldn't be read: "Not known", and the switch can't be used. */
  unknown?: boolean
}

/** "Start Hearthkeep when I sign in to this PC": a labelled switch with no API calls of its own. */
export function StartupSwitch({ state, onChange, busy = false, error = null, unknown = false }: Props) {
  const labelId = useId()
  const hintId = useId()
  const badgeId = useId()
  const elsewhereId = useId()
  const elsewhere = state !== null && state.supported && state.enabled && state.other_data_folder === true
  const checked = state !== null && state.supported && state.enabled && !elsewhere
  const badge = unknown
    ? 'Not known'
    : state === null
      ? ''
      : !state.supported
        ? 'Only on Windows'
        : elsewhere
          ? 'On, for another data folder'
          : state.enabled
            ? 'On'
            : 'Off'
  return (
    <div className="startup-switch" aria-busy={(state === null && !unknown) || busy || undefined}>
      <div className="startup-switch-row">
        <div className="perm-text">
          <b id={labelId}>Start Hearthkeep when I sign in to this PC</b>
          <p className="perm-sub" id={hintId}>
            Hearthkeep starts on its own, with no window, when you sign in to the PC.
          </p>
          {badge && (
            <span id={badgeId} className={`badge${checked ? ' green' : ''}`}>
              {badge}
            </span>
          )}
          {elsewhere && (
            <p className="perm-sub" id={elsewhereId}>
              Switching this on makes it start this Hearthkeep instead.
            </p>
          )}
        </div>
        <button
          type="button"
          role="switch"
          className="switch"
          aria-checked={checked}
          aria-labelledby={labelId}
          aria-describedby={[hintId, badge ? badgeId : '', elsewhere ? elsewhereId : ''].filter(Boolean).join(' ')}
          aria-disabled={busy || undefined}
          disabled={state === null || !state.supported}
          onClick={() => {
            if (!busy) onChange(!checked)
          }}
        >
          <span className="switch-knob" aria-hidden="true" />
        </button>
      </div>
      {error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </div>
      )}
    </div>
  )
}
