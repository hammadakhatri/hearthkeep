import { useEffect, useRef, useState } from 'react'
import { SentTray, useUnreadSent } from '../components/SentTray'
import { Chat } from '../views/Chat'
import { Images } from '../views/Images'
import { Placeholder } from '../views/Placeholder'
import { ToolsPermissions } from '../views/ToolsPermissions'
import { BottomTabs, Sidebar } from './Sidebar'
import { TopBar, useHealth, type HealthState } from './TopBar'
import { DEFAULT_VIEW, VIEWS, type ViewId } from './views'

type Props = {
  initialView?: ViewId
  onSignOut: () => void
}

function modelName(health: HealthState): string | null {
  return 'model' in health ? health.model : null
}

export function AppShell({ initialView = DEFAULT_VIEW, onSignOut }: Props) {
  const [view, setView] = useState<ViewId>(initialView)
  const health = useHealth()
  const scroller = useRef<HTMLDivElement>(null)
  const [unread, setUnread] = useUnreadSent()
  const [trayOpen, setTrayOpen] = useState(false)
  // Bumped when a notice arrives while the tray is open, so it loads what is new.
  const [arrivals, setArrivals] = useState(0)

  // `#sent` (the notice opened a new window) and the worker's `open-sent` message (the app is
  // already open) both open the tray. Only the worker of this origin is believed.
  useEffect(() => {
    const fromHash = () => {
      if (window.location.hash !== '#sent') return
      setTrayOpen(true)
      setArrivals((n) => n + 1)
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
    }
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return
      const data: unknown = event.data
      if (typeof data !== 'object' || data === null || (data as { type?: unknown }).type !== 'open-sent') return
      setTrayOpen(true)
      setArrivals((n) => n + 1)
    }
    fromHash()
    window.addEventListener('hashchange', fromHash)
    const worker = 'serviceWorker' in navigator ? navigator.serviceWorker : undefined
    worker?.addEventListener('message', onMessage)
    return () => {
      window.removeEventListener('hashchange', fromHash)
      worker?.removeEventListener('message', onMessage)
    }
  }, [])

  function select(next: ViewId) {
    if (next !== view && scroller.current) scroller.current.scrollTop = 0
    setView(next)
  }

  return (
    <div className="shell">
      <Sidebar current={view} onSelect={select} onSignOut={onSignOut} />
      <main className="main">
        <TopBar title={VIEWS[view].title} health={health} unread={unread} onOpenSent={() => setTrayOpen(true)} />
        {view === 'chat' ? (
          // Chat scrolls its own thread under a floating composer.
          <div className="view view-fill" key={view}>
            <Chat model={modelName(health)} />
          </div>
        ) : (
          <div className="scroll" ref={scroller}>
            <div className="view" key={view}>
              {/* Views not built yet are placeholders; later updates replace them one by one. */}
              {view === 'tools' ? (
                <ToolsPermissions />
              ) : view === 'images' ? (
                <Images />
              ) : (
                <Placeholder view={view} />
              )}
            </div>
          </div>
        )}
      </main>
      <BottomTabs current={view} onSelect={select} onSignOut={onSignOut} />
      <SentTray open={trayOpen} onClose={() => setTrayOpen(false)} onUnread={setUnread} reloadKey={arrivals} />
    </div>
  )
}
