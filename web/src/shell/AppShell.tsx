import { useRef, useState } from 'react'
import { Chat } from '../views/Chat'
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

  function select(next: ViewId) {
    if (next !== view && scroller.current) scroller.current.scrollTop = 0
    setView(next)
  }

  return (
    <div className="shell">
      <Sidebar current={view} onSelect={select} onSignOut={onSignOut} />
      <main className="main">
        <TopBar title={VIEWS[view].title} health={health} />
        {view === 'chat' ? (
          // Chat scrolls its own thread under a floating composer.
          <div className="view view-fill" key={view}>
            <Chat model={modelName(health)} />
          </div>
        ) : (
          <div className="scroll" ref={scroller}>
            <div className="view" key={view}>
              {/* Views not built yet are placeholders; later updates replace them one by one. */}
              {view === 'tools' ? <ToolsPermissions /> : <Placeholder view={view} />}
            </div>
          </div>
        )}
      </main>
      <BottomTabs current={view} onSelect={select} onSignOut={onSignOut} />
    </div>
  )
}
