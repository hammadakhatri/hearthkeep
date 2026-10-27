/** "How I got this", folded: for each run, what ran, what it printed, how long it took. */

import type { RunDetail } from '../api/runs'

export const SANDBOX_FACTS = 'ran in a sandbox · no internet · read-only files'
export const VISION_FACTS = "Read with the chat model's vision · no internet"
export const OUTPUT_CUT = 'Printed output was cut at 256 KB.'

function seconds(ms: number): string {
  return `${(Math.max(0, ms) / 1000).toFixed(1)} s`
}

function RunAccount({ run, n, many }: { run: RunDetail; n: number; many: boolean }) {
  const vision = run.purpose === 'vision'
  const failed = run.status !== 'ok'
  return (
    <section className="how-run" data-status={run.status}>
      {many && <h4 className="how-run-name">{`Run ${n}`}</h4>}
      {vision ? (
        <>
          <p className="how-label">What it read</p>
          {run.stdout && (
            <pre className="md-code how-out">
              <code>{run.stdout}</code>
            </pre>
          )}
        </>
      ) : (
        <>
          {run.script !== null && (
            <pre className="md-code how-script" data-lang="python" aria-label="Python that ran">
              <code>{run.script}</code>
            </pre>
          )}
          {run.stdout && (
            <>
              <p className="how-label">Printed output</p>
              <pre className="md-code how-out">
                <code>{run.stdout}</code>
              </pre>
            </>
          )}
        </>
      )}
      {run.stdout_truncated && <p className="how-note">{OUTPUT_CUT}</p>}
      {failed && (
        <p className="how-error" role="note">
          {run.error || "This run didn't finish."}
        </p>
      )}
      <p className="how-facts">
        <span>{seconds(run.duration_ms)}</span>
        <span aria-hidden="true"> · </span>
        <span>{vision ? VISION_FACTS : SANDBOX_FACTS}</span>
      </p>
    </section>
  )
}

export function HowIGotThis({ runs }: { runs: RunDetail[] }) {
  if (!runs.length) return null
  return (
    <details className="how">
      <summary>How I got this</summary>
      <div className="how-body">
        {runs.map((run, i) => (
          <RunAccount key={run.id} run={run} n={i + 1} many={runs.length > 1} />
        ))}
      </div>
    </details>
  )
}
