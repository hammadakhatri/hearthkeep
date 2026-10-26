import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ModelInfo, ModelsResponse } from '../api/models'

const KIND: Record<ModelInfo['kind'], string> = {
  chat: 'Chat',
  image: 'Images',
}
const SOURCE: Record<ModelInfo['source'], string> = {
  shipped: 'Shipped',
  owner: 'Yours',
}
const gb = (n: number) => n.toFixed(1)

function DocsLine() {
  return (
    <p className="models-docs">To add a model, add a card file. See docs/models.md in the Hearthkeep repository.</p>
  )
}

function GpuBar({ data }: { data: ModelsResponse['gpu'] | undefined }) {
  if (!data || !(data.total_gb > 0)) return null
  const used = Math.max(0, data.used_gb ?? 0)
  const text = `${gb(used)} of ${gb(data.total_gb)} GB in use`
  const pct = Math.min(100, (used / data.total_gb) * 100)
  return (
    <div className="gpu">
      <p className="gpu-text">
        {text}
        {data.waiting > 0 ? ` · ${data.waiting} waiting` : ''}
      </p>
      <div
        className="gpu-meter"
        role="meter"
        aria-label="GPU memory"
        aria-valuenow={Math.min(used, data.total_gb)}
        aria-valuemin={0}
        aria-valuemax={data.total_gb}
        aria-valuetext={text}
      >
        <span className="gpu-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function ModelList() {
  const [data, setData] = useState<ModelsResponse | null>(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(() => {
    setFailed(false)
    api<ModelsResponse>('/api/models')
      .then(setData)
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  let body
  if (failed) {
    body = (
      <p className="models-quiet">
        Couldn't load models.{' '}
        <button type="button" className="btn ghost" onClick={load}>
          Retry
        </button>
      </p>
    )
  } else if (data === null) {
    body = <p className="models-quiet">Loading models…</p>
  } else if (data.models.length === 0 && data.errors.length === 0) {
    body = (
      <>
        <p className="models-quiet">No models yet.</p>
        <DocsLine />
      </>
    )
  } else {
    body = (
      <>
        {data.models.length > 0 && (
          <ul className="model-list">
            {data.models.map((m) => (
              <li key={m.id} className="model">
                <b className="model-name">{m.name}</b>
                <span className="model-kind">{KIND[m.kind]}</span>
                <ul className="chips" aria-label={`${m.name} capabilities`}>
                  {m.capabilities.map((c) => (
                    <li key={c} className="chip">
                      {c}
                    </li>
                  ))}
                </ul>
                <span className="model-source">{SOURCE[m.source]}</span>
                <span className={`model-status${m.resident ? ' loaded' : ''}`}>
                  <span className="dot" aria-hidden="true" />
                  {m.resident ? 'Loaded' : 'Ready'}
                </span>
              </li>
            ))}
          </ul>
        )}
        {data.errors.length > 0 && (
          <ul className="model-errors" aria-label="Model cards with problems">
            {data.errors.map((e, i) => (
              <li key={`${e.id_or_file}-${i}`}>
                <b>{e.id_or_file}</b> {e.message}
              </li>
            ))}
          </ul>
        )}
        <GpuBar data={data.gpu} />
        <DocsLine />
      </>
    )
  }

  return (
    <section className="models" aria-labelledby="models-title">
      <h3 id="models-title" className="perms-sub-head">
        Models
      </h3>
      {body}
    </section>
  )
}
