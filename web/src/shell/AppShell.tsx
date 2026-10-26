import { useRef, useState, type ReactNode } from 'react'
import { Placeholder } from '../views/Placeholder'
import { BottomTabs, Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { DEFAULT_VIEW, VIEWS, type ViewId } from './views'

type Props = {
  initialView?: ViewId
  onSignOut: () => void
}

function renderView(view: ViewId): ReactNode {
  // Every destination is a placeholder in this release; later updates replace them one by one.
  return <Placeholder view={view} />
}

export function AppShell({ initialView = DEFAULT_VIEW, onSignOut }: Props) {
  const [view, setView] = useState<ViewId>(initialView)
  const scroller = useRef<HTMLDivElement>(null)

  function select(next: ViewId) {
    if (next !== view && scroller.current) scroller.current.scrollTop = 0
    setView(next)
  }

  return (
    <div className="shell">
      <Sidebar current={view} onSelect={select} onSignOut={onSignOut} />
      <main className="main">
        <TopBar title={VIEWS[view].title} />
        <div className="scroll" ref={scroller}>
          <div className="view" key={view}>
            {renderView(view)}
          </div>
        </div>
      </main>
      <BottomTabs current={view} onSelect={select} onSignOut={onSignOut} />
    </div>
  )
}
