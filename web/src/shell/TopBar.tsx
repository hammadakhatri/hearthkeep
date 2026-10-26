import { useEffect, useState } from 'react'
import { api } from '../api/client'

type Health = { status: string; chat_model: string; chat_model_reachable: boolean }

export type HealthState =
  | { state: 'checking' }
  | { state: 'online'; model: string }
  | { state: 'offline'; model: string }
  | { state: 'unreachable' }

const POLL_MS = 15_000

/** Polls /api/health (the server caches it for a few seconds) and re-checks when the tab returns. */
export function useHealth(): HealthState {
  const [health, setHealth] = useState<HealthState>({ state: 'checking' })

  useEffect(() => {
    let alive = true
    const check = async () => {
      try {
        const h = await api<Health>('/api/health')
        if (alive) {
          setHealth(h.chat_model_reachable ? { state: 'online', model: h.chat_model } : { state: 'offline', model: h.chat_model })
        }
      } catch {
        if (alive) setHealth({ state: 'unreachable' })
      }
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
    case 'offline':
      text = 'Chat model offline'
      title = `${health.model} is not answering. Check that your chat server is running.`
      break
    case 'unreachable':
      text = "Can't reach Hearthkeep"
      title = 'The Hearthkeep server is not answering.'
      break
  }
  return (
    <div className="status" role="status" data-state={health.state} title={title}>
      <span className="dot" aria-hidden="true" />
      <span className="status-text">{text}</span>
    </div>
  )
}

export function TopBar({ title }: { title: string }) {
  const health = useHealth()
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
