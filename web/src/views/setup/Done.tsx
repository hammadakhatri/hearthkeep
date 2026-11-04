import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

type Props = {
  /** What is set up, a few words each. */
  setUp: string[]
  /** The downloads that hadn't finished when the passkey was made; the next start carries on with them. */
  unfinished: string[]
  onOpen(): void
  focus: boolean
}

/** Step 7: what's set up, what's still downloading, and "Open Hearthkeep". */
export function Done({ setUp, unfinished, onOpen, focus }: Props) {
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
      {unfinished.length > 0 && (
        <>
          <h3 className="setup-sub">{SETUP_COPY.doneStillDownloading}</h3>
          <ul className="setup-list">
            {unfinished.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
          <p className="setup-text">{SETUP_COPY.doneCarryOn}</p>
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
