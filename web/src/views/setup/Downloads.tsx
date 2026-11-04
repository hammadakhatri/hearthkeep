import type { CatalogItem, Job } from '../../api/models'
import { DownloadList } from '../../components/DownloadList'
import { ConfirmDownload } from '../../components/ModelsPanel'
import { AlertIcon } from '../../shell/icons'
import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

type Props = {
  /** The job setup started (or found running after a reload); null before one starts. */
  job: Job | null
  /** Everything chosen in the chat model and add-ons steps, each file once. */
  plan: CatalogItem[]
  makesDefault: boolean
  starting: boolean
  error: string | null
  onStart(): void
  onRetry(): void
  onCancel(): void
  focus: boolean
  onBack(): void
  onNext(): void
}

/** Step 5: one download for everything chosen, its progress, Retry and Stop. Next is allowed
 *  while it runs. */
export function Downloads({ job, plan, makesDefault, starting, error, onStart, onRetry, onCancel, focus, onBack, onNext }: Props) {
  const nextLabel = job || plan.length === 0 ? 'Next' : 'Skip for now'
  return (
    <StepFrame title={SETUP_COPY.downloadsTitle} focus={focus} onBack={onBack} nextLabel={nextLabel} onNext={onNext}>
      {error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </div>
      )}
      {job ? (
        <>
          {job.state === 'running' && <p className="setup-lede">{SETUP_COPY.keepGoing}</p>}
          <section className="models-downloads" aria-label="Downloads">
            <DownloadList job={job} onRetry={onRetry} onCancel={onCancel} />
          </section>
        </>
      ) : plan.length > 0 ? (
        <ConfirmDownload
          plan={plan}
          makesDefault={makesDefault && plan.some((i) => i.kind === 'chat')}
          busy={starting}
          passkey={false}
          onConfirm={onStart}
        />
      ) : (
        <p className="setup-lede">{SETUP_COPY.nothingChosen}</p>
      )}
    </StepFrame>
  )
}
