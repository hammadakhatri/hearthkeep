import { useEffect, useId, useRef, type ReactNode } from 'react'

type Props = {
  title: string
  /** Move focus to the heading when the step appears (the owner moved to it). */
  focus: boolean
  children: ReactNode
  onBack?: () => void
  /** "Next", or "Skip for now" when nothing on the step was chosen. */
  nextLabel?: string
  onNext?: () => void
  /** Back is held while something on the step is under way. */
  busy?: boolean
}

/** One wizard step: its heading (focused as it appears), its content, then Back and Next. */
export function StepFrame({ title, focus, children, onBack, nextLabel, onNext, busy = false }: Props) {
  const titleId = useId()
  const heading = useRef<HTMLHeadingElement | null>(null)

  useEffect(() => {
    if (focus) heading.current?.focus()
    // Only as the step appears.
  }, [])

  return (
    <section className="setup-step" aria-labelledby={titleId}>
      <h2 id={titleId} ref={heading} className="setup-step-title" tabIndex={-1}>
        {title}
      </h2>
      {children}
      {(onBack || onNext) && (
        <div className="setup-nav">
          {onBack && (
            <button type="button" className="btn ghost" disabled={busy} onClick={onBack}>
              Back
            </button>
          )}
          {onNext && nextLabel && (
            <button type="button" className="btn primary" onClick={onNext}>
              {nextLabel}
            </button>
          )}
        </div>
      )}
    </section>
  )
}
