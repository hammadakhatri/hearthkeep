/**
 * One interactive chart: hover for exact values, and the spec's own zoom and legend params.
 * The chart runtime (Vega) is loaded on first use, in its own chunk.
 */

import { useEffect, useRef, useState } from 'react'
import type { View } from 'vega'

type Runtime = typeof import('./vegaRuntime')

let runtime: Promise<Runtime> | null = null

function loadRuntime(): Promise<Runtime> {
  runtime ??= import('./vegaRuntime').catch((error) => {
    runtime = null // a failed chunk load can be tried again
    throw error
  })
  return runtime
}

/** A file name from the chart's title: letters, digits, spaces and dashes only. */
function pngName(title: string): string {
  const name = title.replace(/[^\p{L}\p{N} _-]+/gu, ' ').replace(/\s+/g, ' ').trim().slice(0, 80)
  return `${name || 'chart'}.png`
}

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.rel = 'noopener'
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

export function ChartView({
  spec,
  title,
  interactive = true,
}: {
  spec: Record<string, unknown>
  title: string
  interactive?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  const view = useRef<View | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const [saving, setSaving] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  // Charts are drawn once scrolled into view (at once where IntersectionObserver is missing).
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === 'undefined')

  useEffect(() => {
    const el = box.current
    if (seen || !el) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer.disconnect()
          setSeen(true)
        }
      },
      { rootMargin: '200px 0px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [seen])

  useEffect(() => {
    const el = box.current
    if (!seen || !el) return
    let alive = true
    let finalize: (() => void) | null = null
    setState('loading')
    loadRuntime()
      .then((rt) => rt.embedChart(el, spec, { interactive }))
      .then((result) => {
        if (!alive) {
          result.finalize()
          return
        }
        finalize = result.finalize
        view.current = result.view
        setState('ready')
      })
      .catch(() => {
        if (alive) setState('failed')
      })
    return () => {
      alive = false
      view.current = null
      finalize?.()
    }
  }, [spec, interactive, seen])

  async function save() {
    if (!view.current || saving) return
    setSaving(true)
    setSaveFailed(false)
    try {
      const rt = await loadRuntime()
      saveBlob(await rt.exportPng(view.current), pngName(title))
    } catch {
      setSaveFailed(true)
    }
    setSaving(false)
  }

  return (
    <figure className="chart" aria-label={title} data-state={state}>
      <figcaption className="chart-title">{title}</figcaption>
      {state === 'failed' && <p className="chart-failed">This chart couldn't be shown.</p>}
      {/* Always mounted, so a new spec after a failure can be drawn into it. */}
      <div
        ref={box}
        className="chart-canvas"
        role="img"
        aria-label={`Chart: ${title}`}
        hidden={state === 'failed'}
        data-static={interactive ? undefined : ''}
      />
      {state !== 'failed' && (
        <div className="chart-actions">
          <button type="button" className="btn chart-save" onClick={() => void save()} disabled={state !== 'ready' || saving}>
            Save as PNG
          </button>
          {saveFailed && (
            <span className="chart-save-failed" role="status">
              Couldn't save the chart.
            </span>
          )}
        </div>
      )}
    </figure>
  )
}
