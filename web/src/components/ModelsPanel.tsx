import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type RefObject } from 'react'
import { isApiError } from '../api/client'
import {
  cancelInstall,
  formatBytes,
  getCatalog,
  getJob,
  retryInstall,
  startInstall,
  switchToExternalChat,
  switchToLocalChat,
  type Catalog,
  type CatalogItem,
  type ChatSwitch,
  type Job,
  type PcInfo,
} from '../api/models'
import { PASSKEY_NOT_CONFIRMED, passkeyErrorMessage } from '../api/webauthn'
import { AlertIcon, InfoIcon, PasskeyIcon } from '../shell/icons'
import { DownloadList, ITEM_ERRORS } from './DownloadList'
import { RestartNotice, useServer } from './RestartNotice'

/** Plain sentences for what the model endpoints can answer. */
export const MODELS_ERRORS = {
  busy: 'A download is already running.',
  doesnt_fit: 'This model needs more graphics memory than this PC has.',
  needs_nvidia: 'Needs an NVIDIA graphics card',
  nothing_to_do: 'Everything here is already installed.',
  unknown_item: "Hearthkeep doesn't know that download. Reload the page and try again.",
  bad_url:
    "That address won't work. Use a model server on this PC: its address starts with http://localhost or http://127.0.0.1.",
  bad_model: "That model name won't work. Type it as the server lists it, without quotes.",
  no_job: 'Hearthkeep restarted, so that download is no longer listed. Download it again to continue.',
  forgotten:
    'The download stopped when Hearthkeep restarted. Download again to continue; nothing that arrived is lost.',
  not_installed: "That model isn't fully installed. Download it again.",
  config_not_updated: ITEM_ERRORS.config_not_updated,
  secrets_unreadable: "Hearthkeep couldn't read its stored secrets.",
  unavailable: "Downloading models isn't available in this copy of Hearthkeep.",
  load_failed: "Couldn't load the models.",
  network: "Can't reach Hearthkeep. Try again in a moment.",
  failed: "That didn't work. Try again.",
  restart: 'Restart Hearthkeep to use it.',
  no_gpu: 'No NVIDIA graphics card found: models run on the processor, more slowly',
} as const

/** The answers from the server that have a sentence of their own. */
const REASONS = new Set([
  'busy',
  'doesnt_fit',
  'nothing_to_do',
  'unknown_item',
  'bad_url',
  'bad_model',
  'no_job',
  'not_installed',
  'config_not_updated',
  'unavailable',
  'network',
] as const)
type Reason = typeof REASONS extends Set<infer R> ? R : never
const POLL_MS = 1000
const gb = (bytes: number) => `${Math.round(bytes / 1e8) / 10}`

/** A plain sentence for anything downloading or switching can throw. */
export function modelsErrorMessage(error: unknown, action: string): string {
  if (isApiError(error, 'no_space')) {
    const body = error.body as { needed_bytes?: number; free_bytes?: number } | null
    return `There isn't enough free space: about ${gb(body?.needed_bytes ?? 0)} GB is needed and ${gb(body?.free_bytes ?? 0)} GB is free.`
  }
  if (isApiError(error, 'step_up_required')) return PASSKEY_NOT_CONFIRMED
  if (isApiError(error, 'secrets_unreadable')) {
    const message = (error.body as { message?: unknown } | null)?.message
    return typeof message === 'string' && message ? message : MODELS_ERRORS.secrets_unreadable
  }
  if (isApiError(error)) {
    return REASONS.has(error.reason as Reason) ? MODELS_ERRORS[error.reason as Reason] : MODELS_ERRORS.failed
  }
  return passkeyErrorMessage(error, action)
}

function pcLine(pc: PcInfo): string {
  if (!pc.nvidia) return MODELS_ERRORS.no_gpu
  const name = pc.gpu_name ?? 'NVIDIA graphics card'
  return pc.vram_gb ? `${name}, ${Math.round(pc.vram_gb)} GB` : name
}

type Section = 'chat' | 'images'

/** A download makes its chat model the one in use, unless the owner chose a server they run. */
function makesDefault(catalog: Catalog | null): boolean {
  return catalog?.chat.source !== 'external'
}

/** What downloading `item` fetches: the recommended chat engine when the server says none is
 *  ready, and the picture maker for an image model when it's missing (the server adds the same). */
function downloadPlan(catalog: Catalog, item: CatalogItem): CatalogItem[] {
  const plan = [item]
  if (item.kind === 'chat' && !catalog.runtime_ready) {
    const engine = catalog.items.find((i) => i.id === catalog.recommended?.runtime)
    if (engine) plan.unshift(engine)
  }
  if (item.kind === 'image') {
    plan.unshift(...catalog.items.filter((i) => i.kind === 'comfyui' && !i.installed))
  }
  return plan
}

function sectionOf(catalog: Catalog | null, job: Job): Section {
  const kinds = new Set(job.items.map((j) => catalog?.items.find((i) => i.id === j.id)?.kind))
  return kinds.has('image') || (kinds.has('comfyui') && !kinds.has('chat')) ? 'images' : 'chat'
}

function Licence({ item }: { item: CatalogItem }) {
  return (
    <a href={item.licence_url} target="_blank" rel="noreferrer noopener">
      Licence: {item.licence}
    </a>
  )
}

type RowProps = {
  item: CatalogItem
  badge?: string
  inUse?: boolean
  why?: string | null
  canDownload: boolean
  onDownload(item: CatalogItem, button: HTMLButtonElement): void
  onUse?: (item: CatalogItem) => void
}

function ModelRow({ item, badge, inUse, why, canDownload, onDownload, onUse }: RowProps) {
  return (
    <li className="device model">
      <div className="device-main">
        <div className="device-text">
          <div className="device-name">
            <b className="model-name">{item.name}</b>
            {badge && <span className="badge green">{badge}</span>}
            {item.installed && <span className="badge">Installed</span>}
          </div>
          {item.note && <p className="model-note">{item.note}</p>}
          <div className="device-meta">
            <span>{formatBytes(item.size_bytes)}</span>
            <Licence item={item} />
          </div>
          {why && <p className="model-why">{why}</p>}
        </div>
        <div className="device-actions">
          {item.installed ? (
            onUse &&
            !inUse && (
              <button type="button" className="btn" aria-label={`Use this: ${item.name}`} onClick={() => onUse(item)}>
                Use this
              </button>
            )
          ) : (
            <button
              type="button"
              className="btn"
              aria-label={`Download ${item.name}`}
              disabled={!canDownload || !!why}
              onClick={(e) => onDownload(item, e.currentTarget)}
            >
              Download
            </button>
          )}
        </div>
      </div>
    </li>
  )
}

type ConfirmProps = {
  plan: CatalogItem[]
  /** The download makes its chat model the one in use. */
  makesDefault: boolean
  busy: boolean
  boxRef: RefObject<HTMLDivElement | null>
  onConfirm(): void
  onCancel(): void
}

/** What will be fetched and its size, before the passkey is asked for. "Up to": a file shared with
 *  an installed model, or one partly downloaded already, isn't fetched again in full. */
function ConfirmDownload({ plan, makesDefault, busy, boxRef, onConfirm, onCancel }: ConfirmProps) {
  const titleId = useId()
  const total = plan.reduce((sum, i) => sum + i.size_bytes, 0)
  return (
    <div ref={boxRef} className="tile model-confirm" role="group" aria-labelledby={titleId} tabIndex={-1}>
      <h3 id={titleId}>Ready to download</h3>
      <ul className="model-confirm-list">
        {plan.map((i) => (
          <li key={i.id}>
            <b>{i.name}</b>
            <span>{formatBytes(i.size_bytes)}</span>
            <Licence item={i} />
          </li>
        ))}
      </ul>
      <p className="model-total">Up to {formatBytes(total)}</p>
      {makesDefault && <p className="perm-sub">It becomes the chat model in use after a restart.</p>}
      <div className="settings-actions">
        <button type="button" className="btn primary" aria-busy={busy || undefined} disabled={busy} onClick={onConfirm}>
          <PasskeyIcon />
          Download with passkey
        </button>
        <button type="button" className="btn ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}

function ErrorNote({ text }: { text: string }) {
  return (
    <div className="note error" role="alert">
      <AlertIcon />
      <span>{text}</span>
    </div>
  )
}

type Props = {
  /** How often a running download is asked about. Tests make it short. */
  pollMs?: number
}

/** Settings › Chat model and Image models: what chat uses, the catalog with the model this PC
 *  suits, downloads with their progress, and a model server the owner already runs. */
export function ModelsPanel({ pollMs = POLL_MS }: Props) {
  const chatTitle = useId()
  const imagesTitle = useId()
  const externalTitle = useId()
  const modelHint = useId()
  const server = useServer()
  const noteRestartNeeded = server?.noteRestartNeeded

  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [watched, setWatched] = useState(false)
  const [confirm, setConfirm] = useState<{ section: Section; item: CatalogItem } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ section: Section; text: string } | null>(null)
  const [restartAt, setRestartAt] = useState<Section | null>(null)
  const [address, setAddress] = useState('')
  const [key, setKey] = useState('')
  const [modelName, setModelName] = useState('')
  // The section whose download the server forgot when it restarted, for a one-line note.
  const [forgotten, setForgotten] = useState<Section | null>(null)
  const jobRef = useRef<Job | null>(null)
  const catalogRef = useRef<Catalog | null>(null)
  jobRef.current = job
  catalogRef.current = catalog

  const confirmRef = useRef<HTMLDivElement | null>(null)
  const downloadsRef = useRef<HTMLElement | null>(null)
  const inUseRef = useRef<HTMLParagraphElement | null>(null)
  const opener = useRef<HTMLButtonElement | null>(null)
  const [focusTo, setFocusTo] = useState<'confirm' | 'downloads' | 'in-use' | 'opener' | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const loadCatalog = useCallback(() => {
    setLoadError(null)
    getCatalog()
      .then((c) => {
        if (!alive.current) return
        setCatalog(c)
        setAddress((a) => a || (c.chat.source === 'external' ? (c.chat.base_url ?? '') : ''))
      })
      .catch((e) => alive.current && setLoadError(isApiError(e, 'unavailable') ? 'unavailable' : 'load_failed'))
  }, [])

  useEffect(() => {
    loadCatalog()
    getJob()
      .then((j) => alive.current && setJob(j))
      .catch(() => {})
  }, [loadCatalog])

  // Ask about a running job every `pollMs`, never while the page is hidden, and stop when it ends.
  const running = job?.state === 'running'
  useEffect(() => {
    if (!running) return
    setWatched(true)
    let live = true
    let timer: number | undefined
    let asking = false
    const hidden = () => document.visibilityState === 'hidden'
    const schedule = () => {
      if (live && !hidden()) timer = window.setTimeout(tick, pollMs)
    }
    async function tick() {
      timer = undefined
      if (!live || hidden()) return
      asking = true
      try {
        const next = await getJob()
        if (!live) return
        if (next === null) {
          // The server forgot the job (it restarted): stop asking and show what is installed now.
          asking = false
          const was = jobRef.current
          setForgotten(was ? sectionOf(catalogRef.current, was) : 'chat')
          setJob(null)
          loadCatalog()
          return
        }
        setJob(next)
      } catch {
        // Keep the last known state and ask again.
      }
      asking = false
      schedule()
    }
    const onVisibility = () => {
      if (hidden()) {
        window.clearTimeout(timer)
        timer = undefined
      } else if (timer === undefined && !asking) {
        void tick()
      }
    }
    schedule()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      live = false
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [running, pollMs, loadCatalog])

  // When a job this page watched ends: fresh catalog, and a restart if models.toml changed.
  const lastState = useRef<Job['state'] | null>(null)
  useEffect(() => {
    const was = lastState.current
    lastState.current = job?.state ?? null
    if (was !== 'running' || !job || job.state === 'running') return
    loadCatalog()
    if (job.restart_needed) {
      setRestartAt(sectionOf(catalog, job))
      noteRestartNeeded?.()
    }
  }, [job, catalog, loadCatalog, noteRestartNeeded])

  useEffect(() => {
    if (!focusTo) return
    const target =
      focusTo === 'confirm'
        ? confirmRef.current
        : focusTo === 'downloads'
          ? downloadsRef.current
          : focusTo === 'in-use'
            ? inUseRef.current
            : opener.current
    // One try: a target that isn't on screen (the list ended, the box closed) isn't waited for.
    target?.focus()
    setFocusTo(null)
  })

  function openConfirm(section: Section, item: CatalogItem, button: HTMLButtonElement) {
    opener.current = button
    setError(null)
    setConfirm({ section, item })
    setFocusTo('confirm')
  }

  function closeConfirm() {
    setConfirm(null)
    setFocusTo('opener')
  }

  async function download() {
    if (!confirm || busy) return
    const { section, item } = confirm
    setBusy(true)
    setError(null)
    setForgotten(null)
    try {
      const started = await startInstall([item.id], makesDefault(catalog))
      if (!alive.current) return
      setConfirm(null)
      setJob(started)
      setFocusTo('downloads')
    } catch (e) {
      if (!alive.current) return
      if (isApiError(e, 'busy')) {
        const current = (e.body as { job?: Job } | null)?.job
        if (current) setJob(current)
      }
      setError({ section, text: modelsErrorMessage(e, 'Download') })
    }
    setBusy(false)
  }

  async function act(section: Section, call: () => Promise<Job | null>) {
    setError(null)
    setForgotten(null)
    try {
      const next = await call()
      if (!alive.current) return
      if (next) setJob(next)
      setFocusTo('downloads')
    } catch (e) {
      if (!alive.current) return
      if (isApiError(e, 'no_job')) {
        // The server restarted and forgot the job: show what is installed now.
        setJob(null)
        loadCatalog()
      }
      setError({ section, text: modelsErrorMessage(e, 'Retry') })
    }
  }

  function switched(result: ChatSwitch) {
    setCatalog((c) =>
      c ? { ...c, chat: { source: result.source, model: result.model, base_url: result.base_url } } : c,
    )
    setRestartAt('chat')
    noteRestartNeeded?.()
    setFocusTo('in-use')
  }

  async function useLocal(item: CatalogItem) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await switchToLocalChat(item.card)
      if (alive.current) switched(result)
    } catch (e) {
      if (alive.current) setError({ section: 'chat', text: modelsErrorMessage(e, 'Changing the chat model') })
    }
    if (alive.current) setBusy(false)
  }

  async function useExternal(event: FormEvent) {
    event.preventDefault()
    const url = address.trim()
    if (!url || busy) return
    setBusy(true)
    setError(null)
    try {
      const name = modelName.trim()
      const result = await switchToExternalChat(url, key ? key : null, name ? name : null)
      if (!alive.current) return
      setKey('')
      switched(result)
    } catch (e) {
      if (!alive.current) return
      setKey('')
      setError({ section: 'chat', text: modelsErrorMessage(e, 'Changing the chat model') })
    }
    if (alive.current) setBusy(false)
  }

  const showJob = job !== null && (job.state !== 'done' || watched)
  const jobSection = job ? sectionOf(catalog, job) : null
  const restartSection = restartAt ?? (server?.info?.restart_reasons?.includes('models') ? 'chat' : null)
  // "Restart now" only when the server's own state is known; otherwise the plain sentence.
  const supervised = server?.info ? server.info.supervised && !server.stopped : false

  function downloads(section: Section) {
    if (!showJob || jobSection !== section || !job) return null
    return (
      <section ref={downloadsRef} className="models-downloads" aria-label="Downloads" tabIndex={-1}>
        <DownloadList
          job={job}
          onRetry={() => void act(section, retryInstall)}
          onCancel={() => void act(section, cancelInstall)}
        />
      </section>
    )
  }

  function restartLine(section: Section) {
    if (restartSection !== section) return null
    if (supervised) return <RestartNotice place="models" />
    return (
      <div className="note info">
        <InfoIcon />
        <span>{MODELS_ERRORS.restart}</span>
      </div>
    )
  }

  function sectionError(section: Section) {
    return error?.section === section ? <ErrorNote text={error.text} /> : null
  }

  function forgottenNote(section: Section) {
    if (forgotten !== section || job !== null) return null
    return (
      <div className="note info">
        <InfoIcon />
        <span>{MODELS_ERRORS.forgotten}</span>
      </div>
    )
  }

  function confirmBox(section: Section) {
    if (confirm?.section !== section || !catalog) return null
    const plan = downloadPlan(catalog, confirm.item)
    return (
      <ConfirmDownload
        plan={plan}
        makesDefault={makesDefault(catalog) && plan.some((i) => i.kind === 'chat')}
        busy={busy}
        boxRef={confirmRef}
        onConfirm={() => void download()}
        onCancel={closeConfirm}
      />
    )
  }

  const canDownload = !running && !busy && confirm === null
  const loading = catalog === null && loadError === null
  const chatItems = catalog?.items.filter((i) => i.kind === 'chat') ?? []
  const engineItems = catalog?.items.filter((i) => i.kind === 'runtime') ?? []
  const imageItems = catalog?.items.filter((i) => i.kind === 'comfyui' || i.kind === 'image') ?? []
  const chat = catalog?.chat
  const inUseName =
    chat?.source === 'local'
      ? (chatItems.find((i) => i.card === chat.model)?.name ?? chat.model)
      : chat?.source === 'external'
        ? 'A model server you run'
        : null
  const runningElsewhere = running && jobSection !== null

  function loadNote(withAlert: boolean) {
    if (!loadError) return null
    if (loadError === 'unavailable') {
      return (
        <div className="note info">
          <InfoIcon />
          <span>{MODELS_ERRORS.unavailable}</span>
        </div>
      )
    }
    return (
      <div className="note error" role={withAlert ? 'alert' : undefined}>
        <AlertIcon />
        <span>{MODELS_ERRORS.load_failed}</span>
        {withAlert && (
          <button type="button" className="btn ghost" onClick={loadCatalog}>
            Retry
          </button>
        )}
      </div>
    )
  }

  function skeleton(label: string) {
    return (
      <div className="device-list skeleton" aria-busy="true" aria-label={label}>
        <span className="bar w30" />
        <span className="bar w60" />
      </div>
    )
  }

  return (
    <>
      <section
        id="settings-chat"
        className="settings-section"
        aria-labelledby={chatTitle}
        aria-busy={busy || undefined}
        tabIndex={-1}
      >
        <h2 id={chatTitle}>Chat model</h2>
        {restartLine('chat')}
        {loadNote(true)}
        {loading && skeleton('Loading chat models')}
        {catalog && (
          <>
            <p className="settings-lede">{pcLine(catalog.pc)}</p>
            <div className="models-in-use">
              <p ref={inUseRef} className="perm-sub" tabIndex={-1}>
                {inUseName ? `In use: ${inUseName}` : 'No chat model is chosen yet.'}
              </p>
              {chat?.source === 'external' && chat.base_url && <p className="folder-path">{chat.base_url}</p>}
            </div>
            {sectionError('chat')}
            {forgottenNote('chat')}
            {downloads('chat')}
            {runningElsewhere && jobSection !== 'chat' && <p className="settings-lede">{MODELS_ERRORS.busy}</p>}
            <ul className="device-list" aria-label="Chat models">
              {chatItems.map((item) => (
                <ModelRow
                  key={item.id}
                  item={item}
                  badge={item.id === catalog.recommended?.chat ? 'Recommended for this PC' : undefined}
                  inUse={chat?.source === 'local' && chat.model === item.card}
                  why={item.fits ? null : MODELS_ERRORS.doesnt_fit}
                  canDownload={canDownload}
                  onDownload={(i, button) => openConfirm('chat', i, button)}
                  onUse={(i) => void useLocal(i)}
                />
              ))}
            </ul>
            <h3 className="models-sub">Chat engine</h3>
            <p className="settings-lede">
              The program that runs the chat model. Downloading a chat model adds the one recommended here if none is
              installed yet.
            </p>
            <ul className="device-list" aria-label="Chat engines">
              {engineItems.map((item) => (
                <ModelRow
                  key={item.id}
                  item={item}
                  badge={item.id === catalog.recommended?.runtime ? 'Recommended for this PC' : undefined}
                  why={item.fits ? null : MODELS_ERRORS.needs_nvidia}
                  canDownload={canDownload}
                  onDownload={(i, button) => openConfirm('chat', i, button)}
                />
              ))}
            </ul>
            {confirmBox('chat')}
            <form className="tile model-external" aria-labelledby={externalTitle} onSubmit={(e) => void useExternal(e)}>
              <h3 id={externalTitle}>Use a model server I already run</h3>
              <p className="perm-sub">
                A program on this PC that already serves a chat model, such as llama.cpp, Ollama or LM Studio. Saving it
                asks for your passkey.
              </p>
              <div className="folder-form">
                <label className="field">
                  <span>Server address</span>
                  <input
                    className="input"
                    type="text"
                    inputMode="url"
                    value={address}
                    onChange={(e) => setAddress(e.target.value)}
                    placeholder="http://127.0.0.1:11434/v1"
                    autoComplete="off"
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    maxLength={200}
                  />
                </label>
                <label className="field">
                  <span>Key (optional)</span>
                  <input
                    className="input"
                    type="password"
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                    autoComplete="off"
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    maxLength={512}
                  />
                </label>
                <label className="field">
                  <span>Model name (optional)</span>
                  <input
                    className="input"
                    type="text"
                    value={modelName}
                    onChange={(e) => setModelName(e.target.value)}
                    aria-describedby={modelHint}
                    autoComplete="off"
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    maxLength={128}
                  />
                </label>
                <button type="submit" className="btn" disabled={busy || !address.trim()}>
                  <PasskeyIcon />
                  Use this server
                </button>
              </div>
              <p className="hint">
                The address usually ends in /v1. Leave the key empty to keep the key for the server you already use.
              </p>
              <p id={modelHint} className="hint">
                The model's name on that server, if it asks for one.
              </p>
            </form>
          </>
        )}
      </section>

      <section
        id="settings-images"
        className="settings-section"
        aria-labelledby={imagesTitle}
        aria-busy={busy || undefined}
        tabIndex={-1}
      >
        <h2 id={imagesTitle}>Image models</h2>
        {restartLine('images')}
        {loadNote(false)}
        {loading && skeleton('Loading image models')}
        {catalog && (
          <>
            <p className="settings-lede">
              Pictures are made by ComfyUI with one of these models, on an NVIDIA graphics card.
            </p>
            {sectionError('images')}
            {forgottenNote('images')}
            {downloads('images')}
            {runningElsewhere && jobSection !== 'images' && <p className="settings-lede">{MODELS_ERRORS.busy}</p>}
            <ul className="device-list" aria-label="Image models">
              {imageItems.map((item) => (
                <ModelRow
                  key={item.id}
                  item={item}
                  why={catalog.pc.nvidia && item.fits ? null : MODELS_ERRORS.needs_nvidia}
                  canDownload={canDownload}
                  onDownload={(i, button) => openConfirm('images', i, button)}
                />
              ))}
            </ul>
            {confirmBox('images')}
          </>
        )}
      </section>
    </>
  )
}
