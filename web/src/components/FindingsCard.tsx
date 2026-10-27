/**
 * The findings card (the answer style the owner chose): the headline with its checked
 * numbers, the charts, what was cleaned or assumed, the problem rows, the output files and a
 * folded "How I got this". Shown for a turn whose steps ran analysis.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getRun, outputDownloadUrl, type NumberCheck, type RunDetail } from '../api/runs'
import type { AssistantItem } from '../views/chatModel'
import { ChartView } from './ChartView'
import { HowIGotThis } from './HowIGotThis'
import { Markdown } from './Markdown'
import { ProblemTable } from './ProblemTable'

export const UNVERIFIED_LINE = "Numbers marked Unverified weren't found in any result."
export const MARK_LEGEND =
  'Underlined numbers: green = computed by code, blue = read from a page; tap a number for details.'

/** Every run id of the turn, in the order its steps made them. */
export function turnRuns(item: AssistantItem): string[] {
  return [...new Set(item.steps.flatMap((step) => step.runs ?? []))]
}

/** The marks for this text: only when the check is of exactly this text. */
function marksFor(text: string, check: NumberCheck | null | undefined) {
  return check && check.text === text ? check.marks : undefined
}

/** The reply's words, with its numbers marked when the server's check is of this exact text. */
export function Headline({ item, className = '' }: { item: AssistantItem; className?: string }) {
  if (!item.text) return null
  const marks = marksFor(item.text, item.check)
  return (
    <>
      <div className={`ai-text ${className}`.trim()} data-testid="assistant-text">
        <Markdown text={item.text} marks={marks} />
      </div>
      {marks && marks.length > 0 && <p className="num-legend">{MARK_LEGEND}</p>}
      {marks?.some((m) => m.kind === 'unverified') && <p className="num-legend num-legend-warn">{UNVERIFIED_LINE}</p>}
    </>
  )
}

/** The saved details of these runs, fetched once each. */
export function useRunDetails(ids: string[]): { runs: RunDetail[]; failed: boolean; retry: () => void } {
  const key = ids.join(',')
  const [byId, setById] = useState<ReadonlyMap<string, RunDetail>>(() => new Map())
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const asked = useRef(new Set<string>())

  useEffect(() => {
    const fresh = (key ? key.split(',') : []).filter((id) => !asked.current.has(id))
    for (const id of fresh) {
      asked.current.add(id)
      getRun(id).then(
        (run) => setById((all) => new Map(all).set(id, run)),
        () => {
          asked.current.delete(id) // asked again on "Try again"
          setFailed(true)
        },
      )
    }
  }, [key, attempt])

  const retry = useCallback(() => {
    setFailed(false)
    setAttempt((n) => n + 1)
  }, [])

  const runs = ids.map((id) => byId.get(id)).filter((run): run is RunDetail => run !== undefined)
  return { runs, failed, retry }
}

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function FindingsCard({
  item,
  runs,
  failed = false,
  onRetry,
}: {
  item: AssistantItem
  runs: RunDetail[]
  failed?: boolean
  onRetry?: () => void
}) {
  const glance = runs.some((run) => run.sections.includes('glance'))
  const charts = runs.flatMap((run) => run.charts)
  const cleaning = [...new Set(runs.flatMap((run) => run.cleaning))]
  const tables = runs.flatMap((run) => run.tables)
  const files = runs.flatMap((run) => run.files)
  return (
    <section className="findings" aria-label="Findings">
      {glance && <h3 className="findings-glance">At a glance</h3>}
      <Headline item={item} className="findings-headline" />
      {charts.length > 0 && (
        <div className="findings-charts">
          {charts.map((chart) => (
            <ChartView key={chart.id} spec={chart.spec} title={chart.title} />
          ))}
        </div>
      )}
      {cleaning.length > 0 && (
        <section className="findings-cleaning findings-block">
          <h4 className="findings-label">Cleaned or assumed</h4>
          <ul>
            {cleaning.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </section>
      )}
      {tables.length > 0 && (
        <div className="findings-tables">
          {tables.map((table) => (
            <ProblemTable key={table.id} table={table} />
          ))}
        </div>
      )}
      {files.length > 0 && (
        <section className="findings-files findings-block">
          <h4 className="findings-label">Files</h4>
          <ul>
            {files.map((file) => (
              <li key={file.id} className="findings-file">
                <bdi className="findings-file-name">{file.name}</bdi>
                <span className="findings-file-size">{size(file.size)}</span>
                <a
                  className="btn findings-file-download"
                  href={outputDownloadUrl(file.id)}
                  download
                  aria-label={`Download ${file.name}`}
                >
                  Download
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
      {failed && (
        <p className="note error findings-failed" role="alert">
          <span>Some results couldn't be loaded.</span>
          {onRetry && (
            <button type="button" className="btn ghost" onClick={onRetry}>
              Try again
            </button>
          )}
        </p>
      )}
      <HowIGotThis runs={runs} />
    </section>
  )
}

/** The card for a turn, fetching its runs. */
export function TurnFindings({ item }: { item: AssistantItem }) {
  const { runs, failed, retry } = useRunDetails(turnRuns(item))
  return <FindingsCard item={item} runs={runs} failed={failed} onRetry={retry} />
}
