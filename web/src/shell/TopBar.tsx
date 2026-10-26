import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ModelsResponse } from '../api/models'

type Health = {
  status: string
  chat_model: string
  chat_model_reachable: boolean
  /** "starts_on_first_use": Hearthkeep runs the model itself and starts it when needed. */
  chat_model_state?: 'ready' | 'offline' | 'starts_on_first_use'
}

export type GpuUse = { used_gb: number; total_gb: number }

export type HealthState =
  | { state: 'checking' }
  | { state: 'online'; model: string; gpu?: GpuUse }
  | { state: 'standby'; model: string; gpu?: GpuUse }
  | { state: 'offline'; model: string; gpu?: GpuUse }
  | { state: 'unreachable' }

const POLL_MS = 15_000

/** Polls /api/health (the server caches it for a few seconds) and re-checks when the tab returns. */
export function useHealth(): HealthState {
  const [health, setHealth] = useState<HealthState>({ state: 'checking' })

  useEffect(() => {
    let alive = true
    const check = async () => {
      // Models are a bonus: a failure there never changes the health state.
      const [h, m] = await Promise.allSettled([api<Health>('/api/health'), api<ModelsResponse>('/api/models')])
      if (!alive) return
      if (h.status === 'rejected') {
        setHealth({ state: 'unreachable' })
        return
      }
      const model = h.value.chat_model
      const g = m.status === 'fulfilled' ? m.value.gpu : undefined
      const gpu =
        g && typeof g.total_gb === 'number' && typeof g.used_gb === 'number'
          ? { used_gb: g.used_gb, total_gb: g.total_gb }
          : undefined
      if (!h.value.chat_model_reachable) setHealth({ state: 'offline', model, gpu })
      else if (h.value.chat_model_state === 'starts_on_first_use') setHealth({ state: 'standby', model, gpu })
      else setHealth({ state: 'online', model, gpu })
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') void check()
    }
    void check()
    const timer = window.setInterval(check, POLL_MS)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  return health
}

function StatusPill({ health }: { health: HealthState }) {
  let text: string
  let title: string
  switch (health.state) {
    case 'checking':
      text = 'Checking…'
      title = 'Checking the chat model'
      break
    case 'online':
      text = health.model
      title = `Chat model ${health.model} is ready`
      break
    case 'standby':
      text = `${health.model} · starts when needed`
      title = `Hearthkeep starts ${health.model} when you send a message`
      break
    case 'offline':
      text = 'Chat model offline'
      title = `${health.model} is not answering. Check that your chat server is running.`
      break
    case 'unreachable':
      text = "Can't reach Hearthkeep"
      title = 'The Hearthkeep server is not answering.'
      break
  }
  const gpu = 'gpu' in health ? health.gpu : undefined
  const gpuText =
    gpu && gpu.total_gb > 0 ? `GPU ${gpu.used_gb.toFixed(1)} of ${gpu.total_gb.toFixed(1)} GB in use` : null
  if (gpuText) title = `${title.replace(/\.$/, '')}. ${gpuText}`
  return (
    <>
      <div
        className="status"
        role="status"
        data-state={health.state}
        title={title}
        aria-describedby={gpuText ? 'status-gpu' : undefined}
      >
        <span className="dot" aria-hidden="true" />
        <span className="status-text">{text}</span>
      </div>
      {/* Outside the live region, so polling never re-announces it. */}
      {gpuText && (
        <span id="status-gpu" className="sr-only">
          {gpuText}
        </span>
      )}
    </>
  )
}

export function TopBar({ title, health }: { title: string; health: HealthState }) {
  return (
    <header className="topbar">
      <div className="topbar-title">
        <span className="mark topbar-mark" aria-hidden="true" />
        <h1>{title}</h1>
      </div>
      <StatusPill health={health} />
    </header>
  )
}
