import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { api, isApiError, post } from '../api/client'
import { subscribeTurn, type AgentEvent, type ApprovalStatus } from '../api/events'
import { ApprovalCard } from '../components/ApprovalCard'
import { Markdown } from '../components/Markdown'
import { WorkedSteps } from '../components/WorkedSteps'
import { Composer } from '../shell/Composer'
import { AlertIcon, ChatsIcon, NewChatIcon } from '../shell/icons'
import {
  applyEvent,
  itemsFromMessages,
  liveTurn,
  setApproval,
  type AssistantItem,
  type ChatSummary,
  type Item,
  type ServerMessage,
} from './chatModel'

const TITLE_CHARS = 60
const STICK_PX = 96
export const LOST_STREAM = 'Lost connection to this reply. Reload to see the latest.'

type Live = { chatId: string; turnId: string }

function sendError(error: unknown): string {
  if (isApiError(error, 'turn_in_progress')) {
    return 'Hearthkeep is still answering in this chat. Wait for it to finish, then send again.'
  }
  if (isApiError(error, 'secrets_unreadable')) {
    return "Hearthkeep couldn't read its stored secrets. Check the server."
  }
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Your message wasn't sent."
  return "Your message couldn't be sent. Try again."
}

function lastUserIndex(messages: ServerMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === 'user') return i
  return -1
}

export function Chat({ model }: { model?: string | null }) {
  const [chats, setChats] = useState<ChatSummary[] | null>(null)
  const [listFailed, setListFailed] = useState(false)
  const [current, setCurrent] = useState<string | null>(null)
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(false)
  const [live, setLive] = useState<Live | null>(null)
  const [draftThink, setDraftThink] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const opening = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  const currentChat = chats?.find((c) => c.id === current) ?? null
  const busy = live !== null && live.chatId === current

  const open = useCallback(async (chat: ChatSummary, keepNotice = false) => {
    const token = ++opening.current
    setCurrent(chat.id)
    setLive(null)
    setItems([])
    setLoading(true)
    setSheetOpen(false)
    if (!keepNotice) setNotice(null)
    stick.current = true
    try {
      const messages = await api<ServerMessage[]>(`/api/chats/${encodeURIComponent(chat.id)}/messages`)
      if (token !== opening.current) return
      if (chat.active_turn) {
        // The running turn replays from its first event, so leave out what it has saved so far.
        const user = lastUserIndex(messages)
        const kept = messages.slice(0, user + 1)
        const startedAt = user >= 0 ? Date.parse(messages[user].created_at) || Date.now() : Date.now()
        setItems([...itemsFromMessages(kept), liveTurn(chat.active_turn, startedAt)])
        setLive({ chatId: chat.id, turnId: chat.active_turn })
      } else {
        setItems(itemsFromMessages(messages))
      }
    } catch {
      if (token === opening.current) setNotice("This chat couldn't be opened. Try again.")
    }
    if (token === opening.current) setLoading(false)
  }, [])

  const loadChats = useCallback(async () => {
    setListFailed(false)
    try {
      const list = await api<ChatSummary[]>('/api/chats')
      setChats(list)
      return list
    } catch {
      setListFailed(true)
      return null
    }
  }, [])

  // Open the chat that is still answering, or else the most recent one.
  useEffect(() => {
    let alive = true
    void loadChats().then((list) => {
      const pick = list?.find((c) => c.active_turn) ?? list?.[0]
      if (alive && pick) void open(pick)
    })
    return () => {
      alive = false
      opening.current += 1
    }
  }, [loadChats, open])

  // Follow the running turn. Reconnects (and de-duplicates) inside subscribeTurn.
  useEffect(() => {
    if (!live) return
    const { chatId, turnId } = live
    const apply = (event: AgentEvent) =>
      setItems((all) =>
        all.map((it) => (it.kind === 'assistant' && it.turnId === turnId ? applyEvent(it, event, Date.now()) : it)),
      )
    const finish = () => setLive((l) => (l?.turnId === turnId ? null : l))
    return subscribeTurn(
      chatId,
      turnId,
      (event) => {
        apply(event)
        if (event.type === 'turn_done' || event.type === 'turn_error') {
          finish()
          setChats((all) => all?.map((c) => (c.id === chatId ? { ...c, active_turn: null } : c)) ?? null)
        }
      },
      0,
      () => {
        // Gave up on the stream (signed out, chat gone, or the server stayed away): say so
        // in the turn and let the owner use the chat again.
        apply({ type: 'turn_error', seq: 0, message: LOST_STREAM })
        finish()
      },
    )
  }, [live])

  // Keep the newest words in view, unless the owner has scrolled up to read.
  useLayoutEffect(() => {
    const el = scroller.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [items])

  function onScroll() {
    const el = scroller.current
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
  }

  function newChat() {
    opening.current += 1
    setCurrent(null)
    setItems([])
    setLive(null)
    setLoading(false)
    setNotice(null)
    setSheetOpen(false)
  }

  async function send(text: string): Promise<boolean> {
    setNotice(null)
    let chatId = current
    try {
      if (chatId === null) {
        const chat = await api<ChatSummary>('/api/chats', post({ think: draftThink }))
        chatId = chat.id
        opening.current += 1
        setCurrent(chat.id)
        setChats((all) => [chat, ...(all ?? [])])
      }
      const { turn_id } = await api<{ turn_id: string }>(
        `/api/chats/${encodeURIComponent(chatId)}/messages`,
        post({ text }),
      )
      stick.current = true
      setItems((all) => [...all, { kind: 'user', key: `u-${turn_id}`, text }, liveTurn(turn_id, Date.now())])
      setLive({ chatId, turnId: turn_id })
      const id = chatId
      setChats(
        (all) =>
          all?.map((c) =>
            c.id === id
              ? { ...c, active_turn: turn_id, title: c.title || text.split(/\s+/).join(' ').slice(0, TITLE_CHARS) }
              : c,
          ) ?? null,
      )
      return true
    } catch (error) {
      setNotice(sendError(error))
      if (isApiError(error, 'turn_in_progress') && chatId) {
        // Another tab or device started a reply here: follow it.
        const chat = (await loadChats())?.find((c) => c.id === chatId)
        if (chat?.active_turn) void open(chat, true)
      }
      return false
    }
  }

  function resolved(key: string, approvalId: string, status: ApprovalStatus) {
    setItems((all) => all.map((it) => (it.kind === 'assistant' && it.key === key ? setApproval(it, approvalId, status) : it)))
  }

  const list = (
    <ChatList
      chats={chats}
      failed={listFailed}
      current={current}
      onOpen={(c) => void open(c)}
      onNew={newChat}
      onRetry={() => void loadChats()}
    />
  )

  return (
    <div className="chat">
      <aside className="chat-side" aria-label="Chats">
        {list}
      </aside>
      <section className="chat-main" aria-label={currentChat?.title || 'New chat'}>
        <div className="chat-bar">
          <button type="button" className="chipbtn" onClick={() => setSheetOpen(true)} aria-haspopup="dialog">
            <ChatsIcon />
            Chats
          </button>
          <span className="chat-bar-title">{currentChat?.title || 'New chat'}</span>
          <button type="button" className="iconbtn" aria-label="New chat" onClick={newChat}>
            <NewChatIcon />
          </button>
        </div>
        <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
          <div className="thread" aria-busy={loading || undefined}>
            {items.length === 0 && !loading && <EmptyChat />}
            {items.map((item) =>
              item.kind === 'user' ? (
                <div key={item.key} className="me">
                  {item.text}
                </div>
              ) : (
                <AssistantTurn
                  key={item.key}
                  item={item}
                  onResolved={(id, status) => resolved(item.key, id, status)}
                />
              ),
            )}
          </div>
        </div>
        <div className="composer-dock">
          {notice && (
            <div className="note error composer-notice" role="alert">
              <AlertIcon />
              <span>{notice}</span>
            </div>
          )}
          <Composer
            onSend={send}
            busy={busy}
            think={currentChat ? currentChat.think : draftThink}
            onThinkChange={setDraftThink}
            thinkLocked={current !== null}
            model={model ?? null}
          />
        </div>
      </section>
      {sheetOpen && <ChatSheet onClose={() => setSheetOpen(false)}>{list}</ChatSheet>}
    </div>
  )
}

function EmptyChat() {
  return (
    <div className="chat-empty">
      <span className="mark chat-empty-mark" aria-hidden="true" />
      <h2>What can I help with?</h2>
      <p>Your assistant works on your own computer. Anything that leaves it waits for your approval.</p>
    </div>
  )
}

function AssistantTurn({
  item,
  onResolved,
}: {
  item: AssistantItem
  onResolved: (approvalId: string, status: ApprovalStatus) => void
}) {
  const streaming = item.state === 'streaming'
  const quiet = streaming && !item.text && !item.reasoning && item.steps.length === 0
  return (
    <div className="ai" data-state={item.state}>
      {item.reasoning && (
        <details className="thinking">
          <summary>Thinking</summary>
          <div className="thinking-body">{item.reasoning}</div>
        </details>
      )}
      <WorkedSteps steps={item.steps} workedMs={item.workedMs} running={streaming} />
      {item.approvals.map(({ request, status }) => (
        <ApprovalCard
          key={request.approval_id}
          request={request}
          status={status}
          onResolved={(s) => onResolved(request.approval_id, s)}
        />
      ))}
      {item.text && (
        <div className="ai-text" data-testid="assistant-text">
          <Markdown text={item.text} />
        </div>
      )}
      {quiet && (
        <div className="typing" role="status" aria-label="Hearthkeep is writing">
          <span />
          <span />
          <span />
        </div>
      )}
      {item.error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{item.error}</span>
        </div>
      )}
    </div>
  )
}

function ChatList({
  chats,
  failed,
  current,
  onOpen,
  onNew,
  onRetry,
}: {
  chats: ChatSummary[] | null
  failed: boolean
  current: string | null
  onOpen: (chat: ChatSummary) => void
  onNew: () => void
  onRetry: () => void
}) {
  return (
    <div className="chat-list">
      <button type="button" className="btn new-chat" onClick={onNew}>
        <NewChatIcon />
        New chat
      </button>
      {failed ? (
        <p className="chat-list-note">
          Couldn't load your chats.{' '}
          <button type="button" className="btn ghost" onClick={onRetry}>
            Retry
          </button>
        </p>
      ) : chats && chats.length > 0 ? (
        <>
          <p className="label chat-list-label">Recent</p>
          <ul>
            {chats.map((chat) => (
              <li key={chat.id}>
                <button
                  type="button"
                  className="chat-link"
                  aria-current={chat.id === current ? 'page' : undefined}
                  onClick={() => onOpen(chat)}
                >
                  <span className="chat-link-title">{chat.title || 'New chat'}</span>
                  {chat.active_turn && <span className="chat-live" title="Answering" aria-label="Answering" />}
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : chats ? (
        <p className="chat-list-note">Your chats will appear here.</p>
      ) : null}
    </div>
  )
}

function ChatSheet({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const sheet = useRef<HTMLDivElement>(null)
  const opener = useRef<Element | null>(null)

  useEffect(() => {
    opener.current = document.activeElement
    sheet.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return () => (opener.current as HTMLElement | null)?.focus?.()
  }, [])

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation()
      onClose()
    }
  }

  return (
    <div className="scrim" onClick={onClose}>
      <div
        ref={sheet}
        className="sheet chat-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="chats-title"
        onKeyDown={onKeyDown}
        onClick={(event) => event.stopPropagation()}
      >
        <span className="grabber" aria-hidden="true" />
        <h2 id="chats-title" className="sheet-title">
          Chats
        </h2>
        {children}
      </div>
    </div>
  )
}
