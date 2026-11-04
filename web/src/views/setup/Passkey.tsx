import { PasskeyIcon } from '../../shell/icons'
import { Note } from '../SignIn'
import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

export const BUSY = 'Busy for a moment, trying again…'

type Props = {
  /** Creating, then waiting for Hearthkeep to start: 'finishing'; it took too long: 'slow'. */
  stage: 'ready' | 'finishing' | 'slow'
  creating: boolean
  busyRetry: boolean
  error: string | null
  onCreate(): void
  focus: boolean
  onBack(): void
}

/** Step 6: the first passkey, as setup has always made it. It can't be skipped. */
export function Passkey({ stage, creating, busyRetry, error, onCreate, focus, onBack }: Props) {
  const ready = stage === 'ready'
  return (
    <StepFrame title={SETUP_COPY.passkeyTitle} focus={focus} onBack={ready ? onBack : undefined} busy={creating}>
      {ready ? (
        <>
          <p className="setup-lede">One last step: create the passkey you'll use to open Hearthkeep on this PC.</p>
          <div className="setup-passkey">
            <p className="hint" role="status" aria-live="polite">
              {busyRetry ? BUSY : ''}
            </p>
            {error && <Note kind="error">{error}</Note>}
            <button
              type="button"
              className="btn primary lg block"
              onClick={onCreate}
              disabled={creating}
              aria-busy={creating}
            >
              <PasskeyIcon />
              {creating ? 'Waiting for your passkey…' : 'Create passkey'}
            </button>
          </div>
        </>
      ) : (
        <p className="setup-lede" role="status" aria-live="polite">
          {stage === 'finishing'
            ? 'Finishing setup…'
            : 'Hearthkeep is taking a while to start. Open it from the Start menu in a minute.'}
        </p>
      )}
    </StepFrame>
  )
}
