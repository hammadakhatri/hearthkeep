import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { api, isApiError, post } from '../api/client'
import { subscribeTurn, type AgentEvent } from '../api/events'
import { ImageGrid, Thumb, type GridImage } from '../components/ImageGrid'
import { ImageViewer, type ViewerImage } from '../components/ImageViewer'
import { Markdown } from '../components/Markdown'
import { AlertIcon, ImagesIcon } from '../shell/icons'
import { LOST_STREAM } from './Chat'
import { applyEvent, liveTurn, turnImages, type AssistantItem, type ChatSummary } from './chatModel'

/** Where the id of the chat the Images view talks to is remembered (if the browser keeps storage). */
export const IMAGES_CHAT_KEY = 'hk.imagesChatId'
export const IMAGES_CHAT_TITLE = 'Images'
export const PAGE_SIZE = 24
/** The setup guide for image models: docs/models.md in the Hearthkeep folder. */
export const MODELS_GUIDE_URL = '/docs/models.md'

export type Shape = 'square' | 'landscape' | 'portrait' | 'banner'

/** The shapes the image tool knows, with the size it renders each one at. */
export const SHAPES: { id: Shape; label: string; width: number; height: number }[] = [
  { id: 'square', label: 'Square', width: 1024, height: 1024 },
  { id: 'landscape', label: 'Landscape', width: 1536, height: 864 },
  { id: 'portrait', label: 'Portrait', width: 864, height: 1536 },
  { id: 'banner', label: 'Banner', width: 1536, height: 640 },
]
const COUNTS = [1, 2, 3, 4]
const ALT_CHARS = 140

type ModelCard = { id: string; name: string; kind: string }
type Models = { state: 'loading' } | { state: 'ready'; cards: ModelCard[] } | { state: 'none' } | { state: 'unknown' }

export type ImageRecord = {
  id: string
  model_id: string | null
  prompt: string | null
  width: number | null
  height: number | null
  created_at: string
  /** Set for images made in this visit, before the server has told us their prompt. */
  alt?: string
}

type Run = { prompt: string; model: string | null; shape: Shape; count: number; item: AssistantItem }
type Live = { chatId: string; turnId: string }

/** The chat message that asks for images: "Create 4 image(s), shape square, using FLUX: …". */
export function composeMessage({
  text,
  count,
  shape,
  model,
}: {
  text: string
  count: number
  shape: Shape
  model: string | null
}): string {
  return `Create ${count} image(s), shape ${shape}${model ? `, using ${model}` : ''}: ${text.trim()}`
}

function storedChat(): string | null {
  try {
    return localStorage.getItem(IMAGES_CHAT_KEY)
  } catch {
    return null
  }
}

function rememberChat(id: string | null) {
  try {
    if (id) localStorage.setItem(IMAGES_CHAT_KEY, id)
    else localStorage.removeItem(IMAGES_CHAT_KEY)
  } catch {
    // No storage (private window, blocked site data): the id lives in memory for this visit.
  }
}

function createError(error: unknown): string {
  if (isApiError(error, 'turn_in_progress')) {
    return 'Hearthkeep is still making your last images. Wait for them to finish, then try again.'
  }
  if (isApiError(error, 'secrets_unreadable')) {
    return "Hearthkeep couldn't read its stored secrets. Check the server."
  }
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Nothing was sent."
  return "That didn't start. Try again."
}

function altFor(record: ImageRecord): string {
  if (record.alt) return record.alt
  const prompt = record.prompt?.trim()
  if (!prompt) return 'Generated image'
  return prompt.length > ALT_CHARS ? `${prompt.slice(0, ALT_CHARS - 1)}…` : prompt
}

/**
 * One entry per image, at the place it first appears; when the server also sent it, its
 * record (with the prompt) replaces the placeholder made while the turn ran.
 */
function unique(records: ImageRecord[]): ImageRecord[] {
  const byId = new Map<string, ImageRecord>()
  for (const r of records) {
    const known = byId.get(r.id)
    if (!known || (known.alt && !r.alt)) byId.set(r.id, r)
  }
  return [...byId.values()]
}

export function Images() {
  const [models, setModels] = useState<Models>({ state: 'loading' })
  const [model, setModel] = useState('auto')
  const [shape, setShape] = useState<Shape>('square')
  const [count, setCount] = useState(4)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [run, setRun] = useState<Run | null>(null)
  const [live, setLive] = useState<Live | null>(null)
  const [gallery, setGallery] = useState<ImageRecord[] | null>(null)
  const [galleryFailed, setGalleryFailed] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [viewing, setViewing] = useState<{ list: 'run' | 'gallery'; index: number } | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const chatId = useRef<string | null>(null)
  const cursor = useRef<string | null>(null)
  const ids = { shape: useId(), count: useId(), text: useId() }

  const cards = models.state === 'ready' ? models.cards : []
  const notSetUp = models.state === 'none'
  const busy = sending || live !== null

  useEffect(() => {
    let alive = true
    api<{ models: ModelCard[] }>('/api/models')
      .then((reply) => {
        const image = (reply.models ?? []).filter((m) => m.kind === 'image')
        if (alive) setModels(image.length ? { state: 'ready', cards: image } : { state: 'none' })
      })
      .catch(() => alive && setModels({ state: 'unknown' }))
    return () => {
      alive = false
    }
  }, [])

  const loadPage = useCallback(async (before: string | null) => {
    const path = `/api/images?limit=${PAGE_SIZE}${before ? `&before=${encodeURIComponent(before)}` : ''}`
    const { images } = await api<{ images: ImageRecord[] }>(path)
    if (images.length) cursor.current = images[images.length - 1].created_at
    setHasMore(images.length === PAGE_SIZE)
    setGallery((all) => unique([...(all ?? []), ...images]))
  }, [])

  const loadFirst = useCallback(() => {
    setGalleryFailed(false)
    loadPage(null).catch(() => setGalleryFailed(true))
  }, [loadPage])

  useEffect(loadFirst, [loadFirst])

  async function loadMore() {
    setLoadingMore(true)
    try {
      await loadPage(cursor.current)
    } catch {
      setNotice("Couldn't load more images. Try again.")
    }
    setLoadingMore(false)
  }

  // Follow the running turn, as Chat does; new images join the gallery as they arrive.
  useEffect(() => {
    if (!live) return
    const { chatId: id, turnId } = live
    const apply = (event: AgentEvent) =>
      setRun((r) => (r && r.item.turnId === turnId ? { ...r, item: applyEvent(r.item, event, Date.now()) } : r))
    const finish = () => setLive((l) => (l?.turnId === turnId ? null : l))
    return subscribeTurn(
      id,
      turnId,
      (event) => {
        apply(event)
        if (event.type === 'attachments') {
          const now = new Date().toISOString()
          const fresh = event.items
            .filter((a) => a.kind === 'image')
            .map((a): ImageRecord => ({
              id: a.id,
              model_id: null,
              prompt: null,
              width: null,
              height: null,
              created_at: now,
              alt: 'New image',
            }))
          setGallery((all) => unique([...fresh, ...(all ?? [])]))
        }
        if (event.type === 'turn_done' || event.type === 'turn_error') finish()
      },
      0,
      () => {
        apply({ type: 'turn_error', seq: 0, message: LOST_STREAM })
        finish()
      },
    )
  }, [live])

  /** Post to the Images chat, creating it on first use and again if it has gone. */
  async function sendToImagesChat(message: string): Promise<Live> {
    let id = chatId.current ?? storedChat()
    for (let attempt = 0; ; attempt++) {
      if (!id) {
        const chat = await api<ChatSummary>('/api/chats', post({ think: false, title: IMAGES_CHAT_TITLE }))
        id = chat.id
        rememberChat(id)
      }
      chatId.current = id
      try {
        const { turn_id } = await api<{ turn_id: string }>(
          `/api/chats/${encodeURIComponent(id)}/messages`,
          post({ text: message }),
        )
        return { chatId: id, turnId: turn_id }
      } catch (error) {
        if (attempt > 0 || !isApiError(error) || error.status !== 404) throw error
        id = null
        chatId.current = null
        rememberChat(null)
      }
    }
  }

  async function create(event?: FormEvent) {
    event?.preventDefault()
    const prompt = text.trim()
    if (!prompt || busy) return
    const card = cards.find((c) => c.id === model) ?? null
    setSending(true)
    setNotice(null)
    try {
      const started = await sendToImagesChat(composeMessage({ text: prompt, count, shape, model: card?.name ?? null }))
      setViewing(null)
      setRun({ prompt, model: card?.name ?? null, shape, count, item: liveTurn(started.turnId, Date.now()) })
      setLive(started)
      setText('')
    } catch (error) {
      setNotice(createError(error))
    }
    setSending(false)
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void create()
    }
  }

  function modelName(id: string | null): string | null {
    if (!id) return null
    return cards.find((c) => c.id === id)?.name ?? id
  }

  const runImages: ViewerImage[] = run
    ? turnImages(run.item).map((id, i) => ({ id, alt: `Generated image ${i + 1}` }))
    : []
  const galleryImages: ViewerImage[] = (gallery ?? []).map((r) => ({
    id: r.id,
    alt: altFor(r),
    width: r.width,
    height: r.height,
    prompt: r.prompt,
    model: modelName(r.model_id),
    createdAt: r.created_at,
  }))
  const viewerImages = viewing?.list === 'run' ? runImages : galleryImages

  function openViewer(list: 'run' | 'gallery', index: number, tile: HTMLElement) {
    opener.current = tile
    setViewing({ list, index })
  }

  const showGallery = !(notSetUp && gallery !== null && gallery.length === 0)

  return (
    <div className="images">
      <header className="images-head">
        <h2>Create an image</h2>
        <p>Made on this PC with your own image models.</p>
      </header>

      {notSetUp ? (
        <div className="images-setup">
          <span className="images-setup-icon" aria-hidden="true">
            <ImagesIcon width={26} height={26} />
          </span>
          <p className="images-setup-title">Image generation isn't set up on this PC yet.</p>
          <p className="images-setup-sub">
            Add an image model card and tell Hearthkeep where ComfyUI lives. The models guide (docs/models.md in
            your Hearthkeep folder) walks through it.
          </p>
          <a className="btn" href={MODELS_GUIDE_URL} target="_blank" rel="noopener noreferrer">
            How to set up image models
          </a>
        </div>
      ) : (
        <form className="describe" onSubmit={create} aria-label="Create images">
          <label className="sr-only" htmlFor={ids.text}>
            Describe the image you want
          </label>
          <textarea
            id={ids.text}
            className="describe-input"
            rows={3}
            value={text}
            placeholder="Describe the image you want…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            maxLength={2000}
            enterKeyHint="go"
          />
          <div className="describe-controls">
            <label className="ctl">
              <span className="label">Model</span>
              <span className="select-wrap">
                <select className="select" value={model} onChange={(e) => setModel(e.target.value)}>
                  <option value="auto">Auto</option>
                  {cards.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </span>
            </label>
            <div className="ctl">
              <span className="label" id={ids.shape}>
                Shape
              </span>
              <div className="seg shapes" role="group" aria-labelledby={ids.shape}>
                {SHAPES.map((s) => (
                  <button key={s.id} type="button" aria-pressed={shape === s.id} onClick={() => setShape(s.id)}>
                    <span
                      className="shape-glyph"
                      style={{ aspectRatio: `${s.width} / ${s.height}` }}
                      aria-hidden="true"
                    />
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="ctl">
              <span className="label" id={ids.count}>
                How many
              </span>
              <div className="seg" role="group" aria-labelledby={ids.count}>
                {COUNTS.map((n) => (
                  <button key={n} type="button" aria-pressed={count === n} onClick={() => setCount(n)}>
                    {n}
                  </button>
                ))}
              </div>
            </div>
            <button type="submit" className="btn primary describe-go" disabled={!text.trim() || busy}>
              Create
            </button>
          </div>
        </form>
      )}

      {notice && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{notice}</span>
        </div>
      )}

      {run && <RunPanel run={run} images={runImages} onOpen={(i, tile) => openViewer('run', i, tile)} />}

      {showGallery && (
        <section className="gallery" aria-labelledby="gallery-title">
          <h2 id="gallery-title" className="gallery-title">
            Your images
          </h2>
          {galleryFailed ? (
            <div className="note error" role="alert">
              <AlertIcon />
              <span>Couldn't load your images.</span>
              <button type="button" className="btn ghost" onClick={loadFirst}>
                Retry
              </button>
            </div>
          ) : gallery === null ? (
            <ul className="imgrid gallery-grid" aria-busy="true" aria-label="Loading images">
              {[0, 1, 2, 3].map((i) => (
                <li key={i} className="imgtile skeleton" aria-hidden="true" />
              ))}
            </ul>
          ) : gallery.length === 0 ? (
            <p className="gallery-empty">Your images will appear here. Describe one above to start.</p>
          ) : (
            <ImageGrid
              className="gallery-grid"
              images={galleryImages}
              onOpen={(i, tile) => openViewer('gallery', i, tile)}
            />
          )}
          {hasMore && !galleryFailed && (
            <div className="gallery-more">
              <button type="button" className="btn" onClick={() => void loadMore()} disabled={loadingMore}>
                Load more
              </button>
            </div>
          )}
        </section>
      )}

      {viewing && viewerImages.length > 0 && (
        <ImageViewer
          images={viewerImages}
          index={viewing.index}
          onIndex={(index) => setViewing((v) => (v ? { ...v, index } : v))}
          onClose={() => setViewing(null)}
          opener={opener.current}
        />
      )}
    </div>
  )
}

/** The images being made now: what was asked, the live tool step, then a hero and its variants. */
function RunPanel({
  run,
  images,
  onOpen,
}: {
  run: Run
  images: GridImage[]
  onOpen: (index: number, tile: HTMLElement) => void
}) {
  const { item } = run
  const streaming = item.state === 'streaming'
  const step = item.steps[item.steps.length - 1]
  const dims = SHAPES.find((s) => s.id === run.shape)!
  const ratio = `${dims.width} / ${dims.height}`
  const total = streaming ? Math.max(run.count, images.length) : images.length
  const [hero, ...rest] = images
  // Room for the variants still coming, so the row does not jump as they arrive.
  const waiting = Math.max(0, total - 1 - rest.length)

  return (
    <section className="run" aria-labelledby="run-title" aria-busy={streaming || undefined}>
      <h2 id="run-title" className="sr-only">
        New images
      </h2>
      <p className="run-prompt">{run.prompt}</p>
      <div className="pick">
        <span className="pill">{run.model ?? 'Auto'}</span>
        {item.text && (
          <div className="pick-why">
            <Markdown text={item.text} />
          </div>
        )}
      </div>
      <p className="step run-step" data-state={step ? step.state : 'running'} role="status">
        <span className="step-mark" aria-hidden="true" />
        {step ? <span>{step.summary}</span> : <span>{streaming ? 'Getting ready…' : 'No images were made.'}</span>}
        {step?.state === 'failed' && <span className="sr-only"> (didn't work)</span>}
      </p>
      {(hero || streaming) && (
      <div className="run-hero" style={{ aspectRatio: ratio, ['--ar' as string]: dims.width / dims.height }}>
        {hero ? (
          <button type="button" className="imgtile run-hero-tile" onClick={(e) => onOpen(0, e.currentTarget)}>
            <Thumb image={hero} eager />
          </button>
        ) : (
          <span className="skeleton run-wait" aria-hidden="true" />
        )}
      </div>
      )}
      {rest.length + waiting > 0 && (
        <ul className="run-variants">
          {rest.map((image, i) => (
            <li key={image.id} style={{ aspectRatio: ratio }}>
              <button type="button" className="imgtile" onClick={(e) => onOpen(i + 1, e.currentTarget)}>
                <Thumb image={image} />
              </button>
            </li>
          ))}
          {Array.from({ length: waiting }, (_, i) => (
            <li key={`wait-${i}`} style={{ aspectRatio: ratio }} aria-hidden="true">
              <span className="skeleton run-wait" />
            </li>
          ))}
        </ul>
      )}
      {item.error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{item.error}</span>
        </div>
      )}
    </section>
  )
}
