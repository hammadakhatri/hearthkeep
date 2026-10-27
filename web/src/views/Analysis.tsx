import { useCallback, useEffect, useId, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import {
  getAnalysis,
  getChartOutput,
  type AnalysisChart,
  type AnalysisChat,
  type ChartOutput,
} from '../api/analysis'
import { api, isApiError } from '../api/client'
import type { ChatFileInfo } from '../api/files'
import { stepUp } from '../api/webauthn'
import { ChartView } from '../components/ChartView'
import { ConfirmDelete } from '../components/ConfirmDelete'
import { FileChips, chipFromInfo } from '../components/FileChips'
import { MARK_LEGEND } from '../components/FindingsCard'
import { Markdown } from '../components/Markdown'
import { AlertIcon, CloseIcon } from '../shell/icons'
import { DELETE_FILE_BODY } from './Chat'

export const ANALYSIS_EMPTY = 'Attach a file to a chat to start. Your files and charts collect here.'
const FOCUSABLE = 'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'

/** True once the element has been scrolled near the screen (at once without IntersectionObserver). */
function useInView(target: RefObject<Element | null>): boolean {
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    const el = target.current
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
  }, [seen, target])
  return seen
}

type LoadOutput = (id: string) => Promise<ChartOutput>

/** Fetches a chart's output through the shared loader. */
function useOutput(id: string, load: LoadOutput, enabled: boolean): ChartOutput | 'failed' | null {
  const [output, setOutput] = useState<ChartOutput | 'failed' | null>(null)
  useEffect(() => {
    if (!enabled) return
    let alive = true
    load(id).then(
      (out) => alive && setOutput(out),
      () => alive && setOutput('failed'),
    )
    return () => {
      alive = false
    }
  }, [enabled, id, load])
  return output
}

/** A chart's thumbnail: fetched and drawn (static) only once scrolled into view. */
function ChartThumb({
  chart,
  load,
  onOpen,
}: {
  chart: AnalysisChart
  load: LoadOutput
  onOpen: (chart: AnalysisChart, opener: HTMLElement) => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const seen = useInView(box)
  const output = useOutput(chart.id, load, seen)

  // The picture is not reachable by keyboard or screen reader: the button below is the control.
  useEffect(() => {
    box.current?.toggleAttribute('inert', true)
  }, [])

  return (
    <li className="analysis-thumb">
      {/* The button below names the chart; the picture is decoration. */}
      <div ref={box} className="analysis-thumb-chart" aria-hidden="true">
        {output && output !== 'failed' ? (
          <ChartView spec={output.spec} title={chart.title} interactive={false} />
        ) : (
          <span className="analysis-thumb-wait" />
        )}
      </div>
      <button
        type="button"
        className="analysis-thumb-open"
        aria-label={`Open ${chart.title} full size`}
        onClick={(event) => onOpen(chart, event.currentTarget)}
      />
      <p className="analysis-thumb-title">
        <bdi>{chart.title}</bdi>
      </p>
      {output === 'failed' && <p className="analysis-thumb-note">This chart couldn't be loaded.</p>}
    </li>
  )
}

/** The full-size chart: interactive, with the reply that went with it and a way back to the chat. */
function ChartDialog({
  chart,
  chatTitle,
  load,
  opener,
  onClose,
  onOpenInChat,
}: {
  chart: AnalysisChart
  chatTitle: string
  load: LoadOutput
  opener: HTMLElement | null
  onClose: () => void
  onOpenInChat: () => void
}) {
  const dialog = useRef<HTMLDivElement>(null)
  const closeButton = useRef<HTMLButtonElement>(null)
  const returnTo = useRef<HTMLElement | null>(opener)
  const titleId = useId()
  const output = useOutput(chart.id, load, true)

  // Focus moves in on open and back to the thumbnail that opened it on close.
  useEffect(() => {
    if (!returnTo.current) returnTo.current = document.activeElement as HTMLElement | null
    closeButton.current?.focus()
    return () => returnTo.current?.focus?.()
  }, [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onClose()
      } else if (event.key === 'Tab') {
        const items = [...(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
        if (!items.length) return
        const active = document.activeElement
        const first = items[0]
        const last = items[items.length - 1]
        const inside = dialog.current?.contains(active) ?? false
        if (event.shiftKey && (active === first || !inside)) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && (active === last || !inside)) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [onClose])

  const loaded = output && output !== 'failed' ? output : null
  const headline = loaded?.headline && loaded.headline.text ? loaded.headline : null

  return createPortal(
    <div className="analysis-scrim" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialog} className="analysis-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="analysis-dialog-head">
          <div>
            <p className="analysis-dialog-chat">
              <bdi>{chatTitle}</bdi>
            </p>
            <h2 id={titleId} className="analysis-dialog-title">
              <bdi>{chart.title}</bdi>
            </h2>
          </div>
          <button ref={closeButton} type="button" className="iconbtn" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </header>
        <div className="analysis-dialog-body">
          {loaded && <ChartView spec={loaded.spec} title={chart.title} />}
          {output === null && (
            <p className="analysis-note" role="status">
              Loading the chart…
            </p>
          )}
          {output === 'failed' && (
            <p className="note error" role="alert">
              <AlertIcon />
              <span>This chart couldn't be loaded.</span>
            </p>
          )}
          {headline && (
            <div className="analysis-headline">
              <div className="ai-text">
                <Markdown text={headline.text} marks={headline.marks} />
              </div>
              {headline.marks.length > 0 && <p className="num-legend">{MARK_LEGEND}</p>}
            </div>
          )}
        </div>
        <footer className="analysis-dialog-foot">
          <button type="button" className="btn blue" onClick={onOpenInChat}>
            Open in chat
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  )
}

/** One chat's section: its title, its files and its chart thumbnails. */
function ChatSection({
  chat,
  load,
  onDeleteFile,
  onOpenChart,
}: {
  chat: AnalysisChat
  load: LoadOutput
  onDeleteFile: (file: ChatFileInfo) => void
  onOpenChart: (chat: AnalysisChat, chart: AnalysisChart, opener: HTMLElement) => void
}) {
  const headingId = headingIdOf(chat.chat_id)
  const title = chat.title || 'New chat'
  return (
    <section className="analysis-chat" aria-labelledby={headingId}>
      <h3 id={headingId} className="analysis-chat-title" tabIndex={-1}>
        <bdi>{title}</bdi>
      </h3>
      <FileChips
        className="analysis-files"
        label={`Files in ${title}`}
        files={chat.files.map(chipFromInfo)}
        onDelete={(key) => {
          const file = chat.files.find((f) => f.id === key)
          if (file) onDeleteFile(file)
        }}
      />
      {chat.charts.length > 0 && (
        <ul className="analysis-charts" aria-label={`Charts in ${title}`}>
          {chat.charts.map((chart) => (
            <ChartThumb key={chart.id} chart={chart} load={load} onOpen={(c, el) => onOpenChart(chat, c, el)} />
          ))}
        </ul>
      )}
    </section>
  )
}

function headingIdOf(chatId: string): string {
  return `analysis-chat-${chatId}`
}

type Opened = { chat: AnalysisChat; chart: AnalysisChart; opener: HTMLElement }

/** Every attached file and every chart, a section per chat, newest first. */
export function Analysis({ onOpenChat }: { onOpenChat: (chatId: string, runId?: string) => void }) {
  const [chats, setChats] = useState<AnalysisChat[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [more, setMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreFailed, setMoreFailed] = useState(false)
  const [opened, setOpened] = useState<Opened | null>(null)
  const [deleting, setDeleting] = useState<ChatFileInfo | null>(null)
  const outputs = useRef(new Map<string, Promise<ChartOutput>>())
  const headingId = useId()
  // Where focus goes once the confirm dialog has closed after a deletion.
  const refocus = useRef<string | null>(null)

  const loadFirst = useCallback(() => {
    setFailed(false)
    getAnalysis().then(
      (page) => {
        setChats(page.chats)
        setMore(page.more)
      },
      () => setFailed(true),
    )
  }, [])

  useEffect(() => {
    loadFirst()
  }, [loadFirst])

  // One request per chart, shared by its thumbnail and its dialog.
  const load = useCallback<LoadOutput>((id) => {
    let found = outputs.current.get(id)
    if (!found) {
      found = getChartOutput(id)
      outputs.current.set(id, found)
      found.catch(() => outputs.current.delete(id))
    }
    return found
  }, [])

  async function loadMore() {
    const last = chats?.[chats.length - 1]
    if (!last || loadingMore) return
    setLoadingMore(true)
    setMoreFailed(false)
    try {
      const page = await getAnalysis(last.updated_at)
      setChats((all) => {
        const seen = new Set((all ?? []).map((c) => c.chat_id))
        return [...(all ?? []), ...page.chats.filter((c) => !seen.has(c.chat_id))]
      })
      setMore(page.more)
    } catch {
      setMoreFailed(true)
    }
    setLoadingMore(false)
  }

  /** Delete with a passkey step-up bound to exactly this file; throws to the dialog. */
  async function confirmDelete(file: ChatFileInfo) {
    const token = await stepUp('delete', undefined, `file:${file.id}`)
    try {
      await api(`/api/files/${encodeURIComponent(file.id)}`, {
        method: 'DELETE',
        body: JSON.stringify({ step_up_token: token }),
      })
    } catch (error) {
      // Already gone (another tab or device deleted it): that is what was asked for.
      if (!isApiError(error, 'not_found')) throw error
    }
    const left = (chats ?? []).find((c) => c.chat_id === file.chat_id)?.files.filter((f) => f.id !== file.id) ?? []
    refocus.current = left.length ? headingIdOf(file.chat_id) : headingId
    // A chat with no file left is no longer in the gallery (the server lists only chats with files).
    setChats((all) =>
      (all ?? [])
        .map((c) => (c.chat_id === file.chat_id ? { ...c, files: c.files.filter((f) => f.id !== file.id) } : c))
        .filter((c) => c.files.length > 0),
    )
    setDeleting(null)
  }

  // After the dialog's own clean-up (which runs first): the deleted file's button is gone.
  useEffect(() => {
    if (deleting || !refocus.current) return
    const id = refocus.current
    refocus.current = null
    document.getElementById(id)?.focus()
  }, [deleting])

  return (
    <div className="analysis">
      <header className="analysis-head">
        <h2 id={headingId} tabIndex={-1}>
          Analysis
        </h2>
        <p>Every file you attached and every chart, grouped by chat.</p>
      </header>
      {failed ? (
        <p className="note error" role="alert">
          <AlertIcon />
          <span>Couldn't load your analysis.</span>
          <button type="button" className="btn ghost" onClick={loadFirst}>
            Try again
          </button>
        </p>
      ) : chats === null ? (
        <p className="analysis-note" role="status">
          Loading…
        </p>
      ) : chats.length === 0 ? (
        <p className="analysis-empty">{ANALYSIS_EMPTY}</p>
      ) : (
        <>
          {chats.map((chat) => (
            <ChatSection
              key={chat.chat_id}
              chat={chat}
              load={load}
              onDeleteFile={setDeleting}
              onOpenChart={(c, chart, opener) => setOpened({ chat: c, chart, opener })}
            />
          ))}
          {moreFailed && (
            <p className="note error" role="alert">
              <AlertIcon />
              <span>Couldn't load more.</span>
            </p>
          )}
          {more && (
            <button type="button" className="btn analysis-more" disabled={loadingMore} onClick={() => void loadMore()}>
              Load more
            </button>
          )}
        </>
      )}
      {opened && (
        <ChartDialog
          chart={opened.chart}
          chatTitle={opened.chat.title || 'New chat'}
          load={load}
          opener={opened.opener}
          onClose={() => setOpened(null)}
          onOpenInChat={() => {
            const { chat, chart } = opened
            setOpened(null)
            onOpenChat(chat.chat_id, chart.run_id)
          }}
        />
      )}
      {deleting && (
        <ConfirmDelete
          key={deleting.id}
          title={`Delete ${deleting.name}?`}
          body={DELETE_FILE_BODY}
          onConfirm={() => confirmDelete(deleting)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  )
}
