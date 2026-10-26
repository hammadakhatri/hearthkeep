import type { Step } from '../views/chatModel'
import { workedLabel } from '../views/chatModel'

/** The tool steps of one turn, folded: "Worked for 14 seconds · 4 steps". */
export function WorkedSteps({ steps, workedMs, running }: { steps: Step[]; workedMs: number; running: boolean }) {
  if (!steps.length) return null
  const busy = running && steps.some((s) => s.state === 'running')
  return (
    <details className="worked">
      <summary>
        {busy ? (
          <span className="worked-live">
            Working · {steps.length} {steps.length === 1 ? 'step' : 'steps'}
          </span>
        ) : (
          workedLabel(workedMs, steps.length)
        )}
      </summary>
      <ol className="steps">
        {steps.map((step) => (
          <li key={step.id} className="step" data-state={step.state}>
            <span className="step-mark" aria-hidden="true" />
            <span>{step.summary}</span>
            {step.state === 'failed' && <span className="sr-only"> (didn't work)</span>}
            {step.state === 'running' && <span className="sr-only"> (running)</span>}
          </li>
        ))}
      </ol>
    </details>
  )
}
