import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { dismissSent, listSent, sentCount, sentDownloadUrl, type SentItemInfo } from '../api/sent'
import { AlertIcon, CloseIcon, DownloadIcon } from '../shell/icons'
import { ChartView } from './ChartView'
import { MARK_LEGEND, UNVERIFIED_LINE } from './FindingsCard'
import { Markdown } from './Markdown'
import { NotificationToggle } from './NotificationToggle'

export const EMPTY_TRAY = 'Nothing here yet. Charts, tables and files sent to this device appear here.'

const REFRESH_MS = 30_000
const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * How many things are waiting in this device's tray. Refreshed every 30 seconds while the page
 * is visible, and when the window gets focus or the page becomes visible again.
 */
export function useUnreadSent(): [number, (n: number) => void] {
  const [unread, setUnread] = useState(0)
  useEffect(() => {
    let alive = true
    const refresh = () => {
      if (document.visibilityState !== 'visible') return
      sentCount().then(
        (n) => alive && setUnread(n),
        () => {}, // keep the last count; the next refresh tries again
      )
    }
    refresh()
    const timer = window.setInterval(refresh, REFRESH_MS)
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      alive = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [])
  return [unread, setUnread]
}

function when(iso: string): string {
  const t = new Date(iso)
  return Number.isNaN(t.getTime()) ? '' : t.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

const KIND_LABEL: Record<SentItemInfo['kind'], string> = {
  chart: 'Chart',
  table: 'Table',
  file: 'File',
  summary: 'Summary',
}

function Body({ item }: { item: SentItemInfo }) {
  if (item.kind === 'chart') {
    return item.chart ? (
      <ChartView spec={item.chart} title={item.title} />
    ) : (
      <p className="sent-gone">This chart isn't available any more.</p>
    )
  }
  if (item.kind === 'summary') {
    const text = item.text ?? ''
    const marks = item.marks ?? []
    return (
      <>
        <div className="ai-text sent-summary">
          <Markdown text={text} marks={marks} />
        </div>
        {marks.length > 0 && <p className="num-legend">{MARK_LEGEND}</p>}
        {marks.some((m) => m.kind === 'unverified') && <p className="num-legend num-legend-warn">{UNVERIFIED_LINE}</p>}
      </>
    )
  }
  return null
}

function Card({ item, onDismiss }: { item: SentItemInfo; onDismiss: () => void }) {
  const headingId = useId()
  return (
    <li className="sent-card" aria-labelledby={headingId} data-kind={item.kind}>
      <div className="sent-card-head">
        <div className="sent-card-title">
          <span className="sent-kind">{KIND_LABEL[item.kind]}</span>
          <b id={headingId}>
            <bdi>{item.title}</bdi>
          </b>
        </div>
        <time className="sent-time" dateTime={item.created_at}>
          {when(item.created_at)}
        </time>
      </div>
      <Body item={item} />
      <div className="sent-card-actions">
        {item.kind !== 'chart' && (
          <a className="btn" href={sentDownloadUrl(item.id)} download aria-label={`Download ${item.title}`}>
            <DownloadIcon />
            Download
          </a>
        )}
        <button type="button" className="btn ghost" aria-label={`Dismiss ${item.title}`} onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </li>
  )
}

/**
 * "Sent to you": the things sent to this device, newest first, with the notifications switch
 * at the bottom. A modal dialog: focus stays inside, Escape closes it, and focus goes back to
 * whatever opened it. `onUnread` reports the unread count whenever it is known.
 */
export function SentTray({
  open,
  onClose,
  onUnread,
  reloadKey = 0,
}: {
  open: boolean
  onClose: () => void
  onUnread: (n: number) => void
  /** Changes when something new has arrived while the tray is open. */
  reloadKey?: number
}) {
  if (!open) return null
  return <TrayDialog onClose={onClose} onUnread={onUnread} reloadKey={reloadKey} />
}

function TrayDialog({
  onClose,
  onUnread,
  reloadKey,
}: {
  onClose: () => void
  onUnread: (n: number) => void
  reloadKey: number
}) {
  const titleId = useId()
  const dialog = useRef<HTMLDivElement>(null)
  const closeButton = useRef<HTMLButtonElement>(null)
  const [items, setItems] = useState<SentItemInfo[] | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const closeRef = useRef(onClose)
  const unreadRef = useRef(onUnread)
  closeRef.current = onClose
  unreadRef.current = onUnread

  // Focus in on open; back to the opener on close (the top bar button after a link or notice).
  useEffect(() => {
    const active = document.activeElement as HTMLElement | null
    const opener = active && active !== document.body ? active : document.querySelector<HTMLElement>('[data-sent-opener]')
    closeButton.current?.focus()
    return () => {
      const target = opener && opener.isConnected ? opener : document.querySelector<HTMLElement>('[data-sent-opener]')
      target?.focus()
    }
  }, [])

  const load = useCallback(() => {
    let live = true
    listSent().then(
      (data) => {
        if (!live) return
        setItems(data.items.filter((item) => !item.dismissed))
        setLoadFailed(false)
        unreadRef.current(data.unread)
      },
      () => live && setLoadFailed(true),
    )
    return () => {
      live = false
    }
  }, [])

  useEffect(() => load(), [load, reloadKey])

  // Escape and the focus trap listen on the document, so they hold even when the focus has
  // dropped to the page (a dismissed card's button is gone).
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
        return
      }
      if (event.key !== 'Tab' || !dialog.current) return
      const focusable = [...dialog.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => !el.closest('[hidden]'))
      if (!focusable.length) {
        event.preventDefault()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const at = document.activeElement
      const inside = !!at && dialog.current.contains(at)
      if (event.shiftKey && (at === first || !inside)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (at === last || !inside)) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  // Optimistic: the card goes at once and comes back (in place) if the server says no.
  function dismiss(item: SentItemInfo) {
    const before = items ?? []
    setNotice(null)
    setItems(before.filter((i) => i.id !== item.id))
    dialog.current?.focus()
    dismissSent(item.id).then(
      () => sentCount().then(onUnread, () => {}),
      () => {
        setItems((now) => {
          const list = now ?? []
          if (list.some((i) => i.id === item.id)) return list
          const index = Math.min(before.findIndex((i) => i.id === item.id), list.length)
          return [...list.slice(0, index), item, ...list.slice(index)]
        })
        setNotice("Couldn't dismiss that. Try again.")
      },
    )
  }

  return (
    <div className="scrim sent-scrim" onClick={onClose}>
      <div
        ref={dialog}
        className="sent-tray"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="sent-head">
          <h2 id={titleId}>Sent to you</h2>
          <button ref={closeButton} type="button" className="icon-btn" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </header>
        <div className="sent-body">
          {notice && (
            <div className="note error" role="alert">
              <AlertIcon />
              <span>{notice}</span>
            </div>
          )}
          {loadFailed && items === null && (
            <div className="note error" role="alert">
              <AlertIcon />
              <span>Couldn't load what was sent to this device.</span>
              <button type="button" className="btn ghost" onClick={() => load()}>
                Try again
              </button>
            </div>
          )}
          {items === null && !loadFailed && (
            <p className="sent-empty" role="status">
              Loading…
            </p>
          )}
          {items !== null && items.length === 0 && <p className="sent-empty">{EMPTY_TRAY}</p>}
          {items !== null && items.length > 0 && (
            <ul className="sent-list">
              {items.map((item) => (
                <Card key={item.id} item={item} onDismiss={() => dismiss(item)} />
              ))}
            </ul>
          )}
        </div>
        <footer className="sent-foot">
          <NotificationToggle />
        </footer>
      </div>
    </div>
  )
}
