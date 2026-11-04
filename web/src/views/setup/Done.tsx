import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

type Props = {
  /** What is set up, a few words each. */
  setUp: string[]
  /** Downloads the next start carries on with (the server's own rule: a running job's items
   *  that weren't stopped or finished), and where their progress shows. */
  carryOn: string[]
  carryOnLine: string
  /** Downloads that were stopped or failed: nothing carries them on. */
  stopped: string[]
  onOpen(): void
  focus: boolean
}

/** Step 7: what's set up, what's still downloading, what was stopped, and "Open Hearthkeep". */
export function Done({ setUp, carryOn, carryOnLine, stopped, onOpen, focus }: Props) {
  return (
    <StepFrame title={SETUP_COPY.doneTitle} focus={focus}>
      <h3 className="setup-sub">{SETUP_COPY.doneSetUp}</h3>
      {setUp.length > 0 ? (
        <ul className="setup-list">
          {setUp.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : (
        <p className="setup-text">{SETUP_COPY.doneNothing}</p>
      )}
      {carryOn.length > 0 && (
        <>
          <h3 className="setup-sub">{SETUP_COPY.doneStillDownloading}</h3>
          <ul className="setup-list">
            {carryOn.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
          <p className="setup-text">{carryOnLine}</p>
        </>
      )}
      {stopped.length > 0 && (
        <>
          <h3 className="setup-sub">{SETUP_COPY.doneNotDownloaded}</h3>
          <ul className="setup-list">
            {stopped.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
          <p className="setup-text">{SETUP_COPY.doneStopped}</p>
        </>
      )}
      <div className="setup-nav">
        <button type="button" className="btn primary" onClick={onOpen}>
          Open Hearthkeep
        </button>
      </div>
    </StepFrame>
  )
}
