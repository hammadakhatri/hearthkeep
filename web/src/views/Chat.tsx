import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { api, isApiError, post } from '../api/client'
import { subscribeTurn, type AgentEvent, type ApprovalStatus } from '../api/events'
import { isPhotoFile, listChatFiles, attachedFileLine, splitAttachments, type ChatFileInfo } from '../api/files'
import { stepUp } from '../api/webauthn'
import { ApprovalCard } from '../components/ApprovalCard'
import { ConfirmDelete } from '../components/ConfirmDelete'
import { ImageGrid, type GridImage } from '../components/ImageGrid'
import { ImageViewer } from '../components/ImageViewer'
import { Markdown } from '../components/Markdown'
import { AttachedFiles, FileChips, chipFromInfo, useChatFiles, type FileChipItem } from '../components/FileChips'
import { attachedPhotoLine, PhotoChip, uploadUrl, usePhoto } from '../components/Photo'
import { WorkedSteps } from '../components/WorkedSteps'
import { Composer } from '../shell/Composer'
import { AlertIcon, ChatsIcon, NewChatIcon, TrashIcon } from '../shell/icons'
import {
  applyEvent,
  itemsFromMessages,
  liveTurn,
  setApproval,
  type AssistantItem,
  type ChatSummary,
  type Item,
  type ServerMessage,
  type Step,
} from './chatModel'

const TITLE_CHARS = 60
const STICK_PX = 96
const LONG_PRESS_MS = 500
const UNNAMED_FILE = 'Attached file'
export const DELETED_FILE = 'Deleted file'

export const DELETE_CHAT_TITLE = 'Delete this chat?'
export const DELETE_CHAT_BODY =
  "This removes its messages, files, analysis, images and anything sent from it. It can't be undone."
export const DELETE_FILE_BODY =
  "This removes the file from this chat. Answers already given keep their results. It can't be undone."

/** What the open confirm dialog would delete. */
type Deleting = { kind: 'chat'; chat: ChatSummary } | { kind: 'file'; id: string; name: string }
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

/**
 * The chat's title from its first message, as the server makes it: the words only, else the first
 * attached file's name, else "Photo".
 */
function titleFor(text: string, firstFileName?: string): string {
  const squash = (s: string) => s.split(/\s+/).join(' ').trim()
  return (squash(splitAttachments(text).text) || squash(firstFileName ?? '')).slice(0, TITLE_CHARS) || 'Photo'
}

/**
 * A chip for a stored file the thread mentions: a placeholder until the chat's file list has its
 * name, and a plain "Deleted file" (nothing to download or delete) once the list is in and lacks it.
 */
function chipFor(id: string, known: ReadonlyMap<string, ChatFileInfo>, missingMeansGone: boolean): FileChipItem {
  const info = known.get(id)
  if (info) return chipFromInfo(info)
  if (missingMeansGone) return { key: id, name: DELETED_FILE, state: 'deleted' }
  return { key: id, id, name: UNNAMED_FILE, state: 'ready' }
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
  const [deleting, setDeleting] = useState<Deleting | null>(null)
  // Chats whose file list has loaded: a file id missing from it is a deleted file.
  const [loaded, setLoaded] = useState<ReadonlySet<string>>(() => new Set())
  // Files deleted here, whether or not their chat's list has loaded.
  const [gone, setGone] = useState<ReadonlySet<string>>(() => new Set())
  // Where focus goes once the confirm dialog has closed after a deletion.
  const refocus = useRef<'new-chat' | 'composer' | null>(null)
  const attach = usePhoto()
  const files = useChatFiles()
  const clearFiles = files.clear
  // What the thread knows about stored files (names, types, sizes), by file id.
  const [known, setKnown] = useState<ReadonlyMap<string, ChatFileInfo>>(() => new Map())
  const creating = useRef<Promise<string> | null>(null)
  const shown = useRef<string | null>(null)
  const listed = useRef(new Set<string>())
  const asked = useRef(new Set<string>())
  const opening = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  const currentChat = chats?.find((c) => c.id === current) ?? null
  const busy = live !== null && live.chatId === current

  const learnFiles = useCallback(async (chatId: string) => {
    if (listed.current.has(chatId)) return
    listed.current.add(chatId)
    try {
      const list = await listChatFiles(chatId)
      setKnown((all) => new Map([...all, ...list.map((f): [string, ChatFileInfo] => [f.id, f])]))
      setLoaded((all) => new Set(all).add(chatId))
    } catch {
      // Try again the next time it is needed.
      listed.current.delete(chatId)
      asked.current.clear()
    }
  }, [])

  const open = useCallback(async (chat: ChatSummary, keepNotice = false) => {
    const token = ++opening.current
    // Files picked for one chat never follow the owner into another, however it was opened.
    if (shown.current !== chat.id) clearFiles()
    shown.current = chat.id
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
      // Reopened from history: the owner's file lines need their names, fetched once per chat.
      if (messages.some((m) => m.role === 'user' && splitAttachments(m.content).files.length)) {
        void learnFiles(chat.id)
      }
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
  }, [clearFiles, learnFiles])

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

  // Files a step brought in or made: look up their names when the thread has not seen them yet
  // (each unknown id is asked for once, so a file that is gone doesn't loop).
  useLayoutEffect(() => {
    if (!current) return
    const unknown = items
      .flatMap((it) => (it.kind === 'assistant' ? it.steps.flatMap((st) => st.files ?? []) : []))
      .filter((id) => !known.has(id) && !asked.current.has(id))
    if (!unknown.length) return
    for (const id of unknown) asked.current.add(id)
    listed.current.delete(current)
    setLoaded((all) => {
      const next = new Set(all)
      next.delete(current)
      return next
    })
    void learnFiles(current)
  }, [items, current, known, learnFiles])

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
    attach.clear()
    files.clear()
    // A pick after this must start its own chat, not join one still being made for the old draft.
    creating.current = null
    shown.current = null
    opening.current += 1
    setCurrent(null)
    setItems([])
    setLive(null)
    setLoading(false)
    setNotice(null)
    setSheetOpen(false)
  }

  /** Start a chat with the draft's Think choice and make it the open one. */
  async function startChat(): Promise<string> {
    const stamp = opening.current
    const chat = await api<ChatSummary>('/api/chats', post({ think: draftThink }))
    // The owner moved on (New chat, another chat) while it was being made: don't pull them back.
    if (stamp !== opening.current) throw new Error('superseded')
    opening.current += 1
    shown.current = chat.id
    setCurrent(chat.id)
    setChats((all) => [chat, ...(all ?? [])])
    return chat.id
  }

  /** Where an upload goes: the open chat, or a new one made first (once, however many files). */
  function chatForUpload(): Promise<string> {
    if (current !== null) return Promise.resolve(current)
    if (creating.current) return creating.current
    const made: Promise<string> = startChat().finally(() => {
      // Only if it is still the one in flight: New chat may have started another.
      if (creating.current === made) creating.current = null
    })
    creating.current = made
    return made
  }

  /** One paperclip for both: photos keep the photo path, everything else is a chat file. */
  function pickFile(file: File) {
    if (isPhotoFile(file)) {
      void attach.pick(file)
      return
    }
    void files.add(file, chatForUpload).then((info) => {
      if (info) setKnown((all) => new Map(all).set(info.id, info))
    })
  }

  async function send(words: string): Promise<boolean> {
    setNotice(null)
    // Photos and files ride along as their own lines; the bubble shows them as thumbnails and chips.
    const photoId = attach.photo.state === 'ready' ? attach.photo.id : null
    const fileIds = files.readyIds
    const text = [words, ...(photoId ? [attachedPhotoLine(photoId)] : []), ...fileIds.map(attachedFileLine)]
      .filter(Boolean)
      .join('\n')
    let chatId = current
    try {
      if (chatId === null) chatId = await (creating.current ?? startChat())
      const { turn_id } = await api<{ turn_id: string }>(
        `/api/chats/${encodeURIComponent(chatId)}/messages`,
        post({ text }),
      )
      stick.current = true
      setItems((all) => [...all, { kind: 'user', key: `u-${turn_id}`, text }, liveTurn(turn_id, Date.now())])
      setLive({ chatId, turnId: turn_id })
      if (photoId) attach.clear()
      if (fileIds.length) files.sent(fileIds)
      const id = chatId
      setChats(
        (all) =>
          all?.map((c) =>
            c.id === id
              ? { ...c, active_turn: turn_id, title: c.title || titleFor(text, known.get(fileIds[0])?.name) }
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

  /** Delete with a passkey step-up bound to exactly this chat or file; throws to the dialog. */
  async function confirmDelete(target: Deleting) {
    const path =
      target.kind === 'chat'
        ? `/api/chats/${encodeURIComponent(target.chat.id)}`
        : `/api/files/${encodeURIComponent(target.id)}`
    const bound = target.kind === 'chat' ? `chat:${target.chat.id}` : `file:${target.id}`
    const token = await stepUp('delete', undefined, bound)
    try {
      await api(path, { method: 'DELETE', body: JSON.stringify({ step_up_token: token }) })
    } catch (error) {
      // Already gone (another tab or device deleted it): that is what was asked for.
      if (!isApiError(error, 'not_found')) throw error
    }
    refocus.current = target.kind === 'chat' ? 'new-chat' : 'composer'
    setDeleting(null)
    if (target.kind === 'chat') {
      const id = target.chat.id
      setChats((all) => all?.filter((c) => c.id !== id) ?? null)
      listed.current.delete(id)
      // It was open: a new, empty chat takes its place.
      if (shown.current === id || current === id) newChat()
    } else {
      const id = target.id
      setGone((all) => new Set(all).add(id))
      setKnown((all) => {
        const next = new Map(all)
        next.delete(id)
        return next
      })
    }
  }

  // After the dialog's own clean-up (which runs first): the deleted thing's button is gone.
  useEffect(() => {
    if (deleting || !refocus.current) return
    const where = refocus.current
    refocus.current = null
    const candidates =
      where === 'composer'
        ? ['.composer textarea']
        : ['.chat-sheet .new-chat', '.chat-side .new-chat', '.chat-bar button[aria-label="New chat"]']
    for (const selector of candidates) {
      const el = document.querySelector<HTMLElement>(selector)
      el?.focus()
      if (el && document.activeElement === el) return
    }
  }, [deleting])

  const chipOf = (id: string) => chipFor(id, known, gone.has(id) || (current !== null && loaded.has(current)))

  function askDeleteFile(id: string) {
    setDeleting({ kind: 'file', id, name: known.get(id)?.name ?? UNNAMED_FILE })
  }

  function resolved(key: string, approvalId: string, status: ApprovalStatus) {
    setItems((all) => all.map((it) => (it.kind === 'assistant' && it.key === key ? setApproval(it, approvalId, status) : it)))
  }

  const list = (
    <ChatList
      chats={chats}
      failed={listFailed}
      current={current}
      onOpen={(c) => {
        // A photo or file picked for one chat doesn't follow the owner into another.
        if (c.id !== current) attach.clear()
        void open(c)
      }}
      onNew={newChat}
      onRetry={() => void loadChats()}
      onDelete={(chat) => setDeleting({ kind: 'chat', chat })}
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
            {items.map((item, index) =>
              item.kind === 'user' ? (
                <UserBubble
                  key={item.key}
                  text={item.text}
                  chipOf={chipOf}
                  onDeleteFile={askDeleteFile}
                  n={index + 1}
                />
              ) : (
                <AssistantTurn
                  key={item.key}
                  item={item}
                  chipOf={chipOf}
                  onDeleteFile={askDeleteFile}
                  n={index + 1}
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
          <PhotoChip attach={attach} alsoAnnounce={files.announcement} />
          <AttachedFiles attach={files} />
          <Composer
            onSend={send}
            busy={busy}
            think={currentChat ? currentChat.think : draftThink}
            onThinkChange={setDraftThink}
            thinkLocked={current !== null}
            model={model ?? null}
            photo={attach.photo}
            files={{ uploading: files.uploading, ready: files.readyIds.length }}
            onPickFile={pickFile}
          />
        </div>
      </section>
      {sheetOpen && <ChatSheet onClose={() => setSheetOpen(false)}>{list}</ChatSheet>}
      {deleting && (
        <ConfirmDelete
          key={deleting.kind === 'chat' ? `chat:${deleting.chat.id}` : `file:${deleting.id}`}
          title={deleting.kind === 'chat' ? DELETE_CHAT_TITLE : `Delete ${deleting.name}?`}
          body={deleting.kind === 'chat' ? DELETE_CHAT_BODY : DELETE_FILE_BODY}
          onConfirm={() => confirmDelete(deleting)}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  )
}

type FileProps = {
  /** The chip for a stored file's id. */
  chipOf: (id: string) => FileChipItem
  /** Asks to delete a stored file (with a passkey). */
  onDeleteFile: (id: string) => void
}

/** The owner's message: any attached photos, then the words, without the attachment lines. */
function UserBubble({ text, chipOf, onDeleteFile, n }: FileProps & { text: string; n: number }) {
  const { photos, files: fileIds, text: words } = splitAttachments(text)
  return (
    <div className="me" data-photos={photos.length || undefined}>
      {photos.map((id, i) => (
        <img key={`${id}-${i}`} className="me-photo" src={uploadUrl(id)} alt="Your photo" loading="lazy" />
      ))}
      <FileChips
        className="me-files"
        label={`Files you sent in message ${n}`}
        files={fileIds.map(chipOf)}
        onDelete={onDeleteFile}
      />
      {words}
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
  chipOf,
  onDeleteFile,
  n,
  onResolved,
}: FileProps & {
  item: AssistantItem
  n: number
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
      <StepImages steps={item.steps} />
      <StepFiles steps={item.steps} chipOf={chipOf} onDeleteFile={onDeleteFile} n={n} />
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

/** The files a turn's steps brought into the chat or made, under the steps. */
function StepFiles({ steps, chipOf, onDeleteFile, n }: FileProps & { steps: Step[]; n: number }) {
  const ids = [...new Set(steps.flatMap((step) => step.files ?? []))]
  if (!ids.length) return null
  return <FileChips
      className="turn-files"
      label={`Files from reply ${n}`}
      files={ids.map(chipOf)}
      onDelete={onDeleteFile}
    />
}

/** The images a turn's steps made: a grid under the steps, opening into the shared viewer. */
function StepImages({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState<number | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const groups: { stepId: string; start: number; images: GridImage[] }[] = []
  const all: GridImage[] = []
  for (const step of steps) {
    if (!step.images?.length) continue
    const start = all.length
    const images = step.images.map((id, i) => ({ id, alt: `Generated image ${start + i + 1}` }))
    groups.push({ stepId: step.id, start, images })
    all.push(...images)
  }
  if (!all.length) return null
  return (
    <>
      {groups.map((group) => (
        <ImageGrid
          key={group.stepId}
          className="turn-images"
          images={group.images}
          onOpen={(i, tile) => {
            opener.current = tile
            setOpen(group.start + i)
          }}
        />
      ))}
      {open !== null && (
        <ImageViewer
          images={all}
          index={open}
          onIndex={setOpen}
          onClose={() => setOpen(null)}
          opener={opener.current}
        />
      )}
    </>
  )
}

function ChatList({
  chats,
  failed,
  current,
  onOpen,
  onNew,
  onRetry,
  onDelete,
}: {
  chats: ChatSummary[] | null
  failed: boolean
  current: string | null
  onOpen: (chat: ChatSummary) => void
  onNew: () => void
  onRetry: () => void
  /** Asks to delete a chat (with a passkey). */
  onDelete: (chat: ChatSummary) => void
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
              <ChatRow
                key={chat.id}
                chat={chat}
                current={chat.id === current}
                onOpen={() => onOpen(chat)}
                onDelete={() => onDelete(chat)}
              />
            ))}
          </ul>
        </>
      ) : chats ? (
        <p className="chat-list-note">Your chats will appear here.</p>
      ) : null}
    </div>
  )
}

/** One chat in the list. Its delete button, a right-click or a long press asks to delete it. */
function ChatRow({
  chat,
  current,
  onOpen,
  onDelete,
}: {
  chat: ChatSummary
  current: boolean
  onOpen: () => void
  onDelete: () => void
}) {
  const timer = useRef<number | null>(null)
  const pressed = useRef(false)
  const title = chat.title || 'New chat'

  function stop() {
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = null
  }

  useEffect(() => stop, [])

  return (
    <li className="chat-row">
      <button
        type="button"
        className="chat-link"
        aria-current={current ? 'page' : undefined}
        onClick={() => {
          // The long press already asked to delete: this tap is its end, not an open.
          if (pressed.current) {
            pressed.current = false
            return
          }
          onOpen()
        }}
        onContextMenu={(event) => {
          event.preventDefault()
          stop()
          onDelete()
        }}
        onPointerDown={(event) => {
          pressed.current = false
          if (event.pointerType !== 'touch') return
          stop()
          timer.current = window.setTimeout(() => {
            timer.current = null
            pressed.current = true
            onDelete()
          }, LONG_PRESS_MS)
        }}
        onPointerUp={stop}
        onPointerLeave={stop}
        onPointerCancel={stop}
      >
        <span className="chat-link-title">{title}</span>
        {chat.active_turn && <span className="chat-live" title="Answering" aria-label="Answering" />}
      </button>
      <button
        type="button"
        className="iconbtn chat-row-delete"
        aria-label={`Delete chat ${title}`}
        title="Delete"
        onClick={onDelete}
      >
        <TrashIcon />
      </button>
    </li>
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
