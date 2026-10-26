import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { MoreIcon, SignOutIcon } from './icons'
import { FOOTER_VIEWS, MAIN_VIEWS, MORE_VIEWS, TAB_VIEWS, VIEWS, type ViewId } from './views'

type NavProps = {
  current: ViewId
  onSelect: (view: ViewId) => void
  onSignOut: () => void
}

function NavButton({ view, current, onSelect }: { view: ViewId } & Pick<NavProps, 'current' | 'onSelect'>) {
  const { title, icon: Icon } = VIEWS[view]
  return (
    <button
      type="button"
      className="navbtn"
      data-tip={title}
      aria-current={view === current ? 'page' : undefined}
      onClick={() => onSelect(view)}
    >
      <Icon />
      <span className="lbl">{title}</span>
    </button>
  )
}

/** Wide screens: the brand, six destinations, and Tools & permissions at the bottom. */
export function Sidebar({ current, onSelect, onSignOut }: NavProps) {
  return (
    <nav className="side" aria-label="Hearthkeep">
      <div className="brand">
        <span className="mark" aria-hidden="true" />
        <span className="lbl">Hearthkeep</span>
      </div>
      <div className="side-group">
        {MAIN_VIEWS.map((view) => (
          <NavButton key={view} view={view} current={current} onSelect={onSelect} />
        ))}
      </div>
      <div className="side-group foot">
        {FOOTER_VIEWS.map((view) => (
          <NavButton key={view} view={view} current={current} onSelect={onSelect} />
        ))}
      </div>
      <button type="button" className="navbtn quiet" data-tip="Sign out" onClick={onSignOut}>
        <SignOutIcon />
        <span className="lbl">Sign out</span>
      </button>
    </nav>
  )
}

/** Phones (below 560 px): four tabs and More. */
export function BottomTabs({ current, onSelect, onSignOut }: NavProps) {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreButton = useRef<HTMLButtonElement>(null)
  const inMore = MORE_VIEWS.includes(current)

  function closeMore() {
    setMoreOpen(false)
    moreButton.current?.focus()
  }

  return (
    <>
      <nav className="tabs" aria-label="Tabs">
        {TAB_VIEWS.map((view) => {
          const { title, icon: Icon } = VIEWS[view]
          return (
            <button
              key={view}
              type="button"
              className="tab"
              aria-current={view === current ? 'page' : undefined}
              onClick={() => onSelect(view)}
            >
              <Icon />
              <span>{title}</span>
            </button>
          )
        })}
        <button
          ref={moreButton}
          type="button"
          className="tab"
          aria-current={inMore ? 'page' : undefined}
          aria-haspopup="dialog"
          aria-expanded={moreOpen}
          onClick={() => setMoreOpen(true)}
        >
          <MoreIcon />
          <span>More</span>
        </button>
      </nav>
      {moreOpen && (
        <MoreSheet
          current={current}
          onClose={closeMore}
          onSelect={(view) => {
            setMoreOpen(false)
            onSelect(view)
          }}
          onSignOut={() => {
            setMoreOpen(false)
            onSignOut()
          }}
        />
      )}
    </>
  )
}

function MoreSheet({ current, onSelect, onSignOut, onClose }: NavProps & { onClose: () => void }) {
  const sheet = useRef<HTMLDivElement>(null)

  useEffect(() => {
    sheet.current?.querySelector<HTMLButtonElement>('button')?.focus()
  }, [])

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation()
      onClose()
      return
    }
    // Keep Tab inside the sheet while it is open.
    if (event.key === 'Tab' && sheet.current) {
      const buttons = Array.from(sheet.current.querySelectorAll<HTMLButtonElement>('button'))
      const first = buttons[0]
      const last = buttons[buttons.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
  }

  return (
    <div className="scrim" onClick={onClose}>
      <div
        ref={sheet}
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="more-title"
        onKeyDown={onKeyDown}
        onClick={(event) => event.stopPropagation()}
      >
        <span className="grabber" aria-hidden="true" />
        <h2 id="more-title" className="sheet-title">
          More
        </h2>
        <div className="sheet-list">
          {MORE_VIEWS.map((view) => (
            <NavButton key={view} view={view} current={current} onSelect={onSelect} />
          ))}
          <button type="button" className="navbtn quiet" onClick={onSignOut}>
            <SignOutIcon />
            <span className="lbl">Sign out</span>
          </button>
        </div>
      </div>
    </div>
  )
}
