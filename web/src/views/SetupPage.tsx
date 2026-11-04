import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Addon } from '../api/addons'
import { isApiError } from '../api/client'
import type { Catalog, CatalogItem, Job } from '../api/models'
import { page, waitForSetupDone, type WaitOptions } from '../api/server'
import {
  readSetupSecret,
  setupAddons,
  setupDataFolder,
  setupEnrollmentCode,
  setupModelsApi,
  setupPc,
  setupSandboxApi,
  setupStartup,
  setupStatus,
  waitForMove,
  type SetupPc,
} from '../api/setup'
import { enroll, passkeyErrorMessage } from '../api/webauthn'
import { downloadPlanFor, makesDefault, modelsErrorMessage, useJobPolling } from '../components/ModelsPanel'
import { STARTUP_FAILED } from '../components/StartupSwitch'
import { Stepper } from '../components/Stepper'
import { AddOns } from './setup/AddOns'
import { ChatModel } from './setup/ChatModel'
import { carryOnLine, notEnoughSpace, SETUP_COPY } from './setup/copy'
import { Done } from './setup/Done'
import { Downloads } from './setup/Downloads'
import { BUSY, Passkey } from './setup/Passkey'
import { guessDeviceName } from './SignIn'
import { Welcome } from './setup/Welcome'
import { YourPc } from './setup/YourPc'

type Stage = 'no-secret' | 'checking' | 'ended' | 'away' | 'ready' | 'finishing' | 'slow'

const ENDED = 'This setup link has ended. Double-click Setup.cmd in the Hearthkeep folder to start again.'

/** How long to wait after a 429 before asking again. Other programs on this PC share the limit. */
const BUSY_RETRY_MS = 5000
const POLL_MS = 1000

const STEPS = ['Welcome', 'Your PC', 'Chat model', 'Add-ons', 'Downloads', 'Passkey', 'Done'] as const
const PC_STEP = 1
const PASSKEY_STEP = 5
const DONE_STEP = 6

// The step on screen and what was chosen, for a reload in this tab. Never the secret.
const STEP_KEY = 'hk.setup.step'
const CHOICES_KEY = 'hk.setup.choices'

type Choices = {
  /** Catalog item ids for the one download the Downloads step starts. */
  chosen: string[]
  /** Start at login: on unless the owner turns it off (the server's default too). */
  startup: boolean
}

function readStep(): number {
  try {
    const n = Number(window.sessionStorage.getItem(STEP_KEY))
    // Done is never restored: by then setup is over.
    return Number.isInteger(n) && n >= 0 && n <= PASSKEY_STEP ? n : 0
  } catch {
    return 0
  }
}

function readChoices(): Choices {
  const fallback: Choices = { chosen: [], startup: true }
  try {
    const raw = JSON.parse(window.sessionStorage.getItem(CHOICES_KEY) ?? 'null') as Partial<Choices> | null
    if (!raw || typeof raw !== 'object') return fallback
    const chosen = Array.isArray(raw.chosen) ? raw.chosen.filter((id): id is string => typeof id === 'string') : []
    return { chosen: chosen.slice(0, 10), startup: raw.startup !== false }
  } catch {
    return fallback
  }
}

function remember(key: string, value: string | null) {
  try {
    if (value === null) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, value)
  } catch {
    // Storage is off: a reload starts at the beginning, nothing else changes.
  }
}

type Props = {
  /** Setup is over (or was never on): go to the normal app. */
  onLeave: () => void
  retryMs?: number
  /** How often a running download is asked about. */
  pollMs?: number
  /** How long, and how often, to wait for the server after a data-folder move. */
  moveWait?: WaitOptions
}

/** `/setup`: the setup wizard, with the secret from the setup link. Welcome, this PC, the chat
 *  model, add-ons, one download for everything chosen, the first passkey, then Done. */
export function SetupPage({ onLeave, retryMs = BUSY_RETRY_MS, pollMs = POLL_MS, moveWait }: Props) {
  // Read once: reading clears it from the address bar, so a second read would find nothing.
  const firstRead = useRef<{ value: string | null } | null>(null)
  if (firstRead.current === null) firstRead.current = { value: readSetupSecret() }
  // Held in memory only, never rendered or stored. A setup link opened again in this tab replaces it.
  const [secret, setSecret] = useState(firstRead.current.value)

  const [stage, setStage] = useState<Stage>(secret ? 'checking' : 'no-secret')
  const [busyRetry, setBusyRetry] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const alive = useRef(true)
  const leave = useRef(onLeave)
  leave.current = onLeave

  const [step, setStep] = useState(readStep)
  // Focus goes to a step's heading only when the owner moved to it, not on the first screen.
  const stepChanged = useRef(false)
  const [choices, setChoices] = useState(readChoices)

  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [catalogFailed, setCatalogFailed] = useState(false)
  const [addons, setAddons] = useState<Addon[] | null>(null)
  const [addonsFailed, setAddonsFailed] = useState(false)
  const [pc, setPc] = useState<SetupPc | null>(null)
  const [pcFailed, setPcFailed] = useState(false)
  const [moving, setMoving] = useState(false)
  const [job, setJob] = useState<Job | null>(null)
  const [starting, setStarting] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [chatBusy, setChatBusy] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
  const [chatNotice, setChatNotice] = useState<string | null>(null)
  const [startupBusy, setStartupBusy] = useState(false)
  const [startupError, setStartupError] = useState<string | null>(null)
  const [passkeyMade, setPasskeyMade] = useState(false)

  const modelsApi = useMemo(() => (secret ? setupModelsApi(secret) : null), [secret])
  const sandboxApi = useMemo(() => (secret ? setupSandboxApi(secret) : null), [secret])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // A setup link opened in this tab again only changes the fragment: take it, clear it, check it.
  useEffect(() => {
    const onHash = () => {
      const next = readSetupSecret()
      if (!next) return
      setSecret(next)
      setError(null)
      setAttempt((n) => n + 1)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    if (!passkeyMade) remember(STEP_KEY, String(step))
  }, [step, passkeyMade])

  useEffect(() => {
    if (!passkeyMade) remember(CHOICES_KEY, JSON.stringify(choices))
  }, [choices, passkeyMade])

  /** `call`, asked again after a pause while the server says it is busy (429). */
  async function patiently<T>(call: () => Promise<T>, live: () => boolean = () => alive.current): Promise<T> {
    for (;;) {
      try {
        const result = await call()
        if (live()) setBusyRetry(false)
        return result
      } catch (e) {
        if (!isApiError(e) || e.status !== 429 || !live()) throw e
        setBusyRetry(true)
        await new Promise((resolve) => window.setTimeout(resolve, retryMs))
        if (!live()) throw e
      }
    }
  }

  /** Setup is over (404) or the link no longer works (403): the whole page says so. */
  function setupGone(e: unknown, { over = true } = {}): boolean {
    if (!alive.current) return true
    if (over && isApiError(e) && e.status === 404) {
      leave.current()
      return true
    }
    if (isApiError(e) && e.status === 403) {
      setStage('ended')
      return true
    }
    return false
  }

  useEffect(() => {
    if (!secret) return
    let live = true
    setStage('checking')
    patiently(() => setupStatus(secret), () => live)
      .then(() => live && setStage('ready'))
      .catch((e: unknown) => {
        if (!live) return
        if (isApiError(e) && e.status === 404) leave.current()
        else if (isApiError(e) && e.status === 403) setStage('ended')
        else setStage('away')
      })
    return () => {
      live = false
    }
    // `patiently` only uses state setters and `retryMs`; a new attempt is the Try again button.
  }, [secret, attempt])

  // ---- what the steps show --------------------------------------------------------------------

  const loadCatalog = useCallback(() => {
    if (!modelsApi) return
    setCatalogFailed(false)
    patiently(() => modelsApi.getCatalog())
      .then((c) => alive.current && setCatalog(c))
      .catch((e) => !setupGone(e, { over: false }) && setCatalogFailed(true))
    // `patiently` and `setupGone` only use refs, setters and `retryMs`.
  }, [modelsApi])

  const loadAddons = useCallback(
    (fresh = false) => {
      if (!secret) return
      setAddonsFailed(false)
      patiently(() => setupAddons(secret, fresh))
        .then((r) => alive.current && setAddons(r.addons))
        .catch((e) => !setupGone(e, { over: false }) && setAddonsFailed(true))
    },
    [secret],
  )

  const loadPc = useCallback(async () => {
    if (!secret) return
    setPcFailed(false)
    try {
      const info = await patiently(() => setupPc(secret))
      if (alive.current) setPc(info)
    } catch (e) {
      if (!setupGone(e, { over: false })) setPcFailed(true)
    }
  }, [secret])

  const ready = stage === 'ready'
  useEffect(() => {
    if (!ready || !modelsApi) return
    loadCatalog()
    loadAddons()
    // A download that is already running (the page was reloaded) is shown, never started again.
    patiently(() => modelsApi.getJob())
      .then((j) => alive.current && j && setJob(j))
      .catch((e) => setupGone(e, { over: false }))
  }, [ready, modelsApi, loadCatalog, loadAddons])

  useEffect(() => {
    if (ready && step === PC_STEP && pc === null && !pcFailed) void loadPc()
  }, [ready, step, pc, pcFailed, loadPc])

  // The download keeps going on every step until the passkey is made (setup's server then stops).
  useJobPolling(
    job?.state === 'running' && ready && !passkeyMade && modelsApi !== null,
    pollMs,
    () => modelsApi!.getJob(),
    setJob,
    () => setJob(null),
  )

  function go(next: number) {
    stepChanged.current = true
    setStep(next)
  }

  // ---- this PC and the data folder ------------------------------------------------------------

  async function move(path: string): Promise<string | null> {
    if (!secret) return null
    try {
      await patiently(() => setupDataFolder(secret, path))
    } catch (e) {
      const body = isApiError(e) ? (e.body as { message?: unknown; free_gb?: number; needed_gb?: number } | null) : null
      if (isApiError(e, 'bad_folder')) {
        return typeof body?.message === 'string' && body.message ? body.message : SETUP_COPY.moveFailed
      }
      if (isApiError(e, 'not_enough_space')) return notEnoughSpace(body?.needed_gb ?? 5, body?.free_gb ?? 0)
      if (isApiError(e, 'moving')) return SETUP_COPY.dataMoving
      if (isApiError(e, 'too_late')) {
        void loadPc()
        return SETUP_COPY.tooLate
      }
      if (setupGone(e)) return null
      return SETUP_COPY.moveFailed
    }
    // The server stops, sets up again in the new folder and comes back with the same secret.
    setMoving(true)
    const back = await waitForMove(secret, path, moveWait)
    if (!alive.current) return null
    if (!back) {
      setMoving(false)
      setStage('away')
      return null
    }
    try {
      await patiently(() => setupStatus(secret))
    } catch (e) {
      setMoving(false)
      if (!setupGone(e)) setStage('away')
      return null
    }
    await loadPc()
    loadCatalog()
    if (alive.current) setMoving(false)
    return null
  }

  // ---- choosing models ------------------------------------------------------------------------

  function kindOf(id: string) {
    return catalog?.items.find((i) => i.id === id)?.kind
  }

  function choose(item: CatalogItem) {
    setChatNotice(null)
    setChoices((c) => {
      const had = c.chosen.includes(item.id)
      let chosen = c.chosen.filter((id) => id !== item.id)
      // One chat model: choosing another replaces it.
      if (!had && item.kind === 'chat') chosen = chosen.filter((id) => kindOf(id) !== 'chat')
      if (!had) chosen = [...chosen, item.id]
      return { ...c, chosen }
    })
  }

  function chatChanged(result: { source: Catalog['chat']['source']; model: string | null; base_url: string | null }, notice: string) {
    setCatalog((c) => (c ? { ...c, chat: { source: result.source, model: result.model, base_url: result.base_url } } : c))
    // Chat is settled: no chat model is downloaded for it.
    setChoices((c) => ({ ...c, chosen: c.chosen.filter((id) => kindOf(id) !== 'chat') }))
    setChatNotice(notice)
  }

  async function changeChat(call: () => Promise<{ source: Catalog['chat']['source']; model: string | null; base_url: string | null }>, notice: string) {
    setChatBusy(true)
    setChatError(null)
    setChatNotice(null)
    try {
      const result = await patiently(call)
      if (alive.current) chatChanged(result, notice)
    } catch (e) {
      if (setupGone(e)) return
      setChatError(modelsErrorMessage(e, 'Changing the chat model'))
    }
    if (alive.current) setChatBusy(false)
  }

  // ---- add-ons --------------------------------------------------------------------------------

  async function changeStartup(enabled: boolean) {
    if (!secret) return
    setStartupBusy(true)
    setStartupError(null)
    try {
      const result = await patiently(() => setupStartup(secret, enabled))
      if (alive.current) setChoices((c) => ({ ...c, startup: result.enabled }))
    } catch (e) {
      if (setupGone(e)) return
      setStartupError(STARTUP_FAILED)
    }
    if (alive.current) setStartupBusy(false)
  }

  // ---- downloads ------------------------------------------------------------------------------

  const chosenItems = catalog?.items.filter((i) => choices.chosen.includes(i.id) && !i.installed) ?? []
  const plan = catalog ? downloadPlanFor(catalog, chosenItems) : []

  function downloadFailed(e: unknown, action: string) {
    if (setupGone(e)) return
    if (isApiError(e, 'busy')) {
      const current = (e.body as { job?: Job } | null)?.job
      if (current) setJob(current)
    }
    if (isApiError(e, 'no_job')) setJob(null)
    setDownloadError(isApiError(e, 'moving') ? SETUP_COPY.dataMoving : modelsErrorMessage(e, action))
  }

  async function startDownloads() {
    if (!modelsApi || !catalog || starting || job || plan.length === 0) return
    setStarting(true)
    setDownloadError(null)
    try {
      const started = await patiently(() => modelsApi.startInstall(plan.map((i) => i.id), makesDefault(catalog)))
      if (alive.current) setJob(started)
    } catch (e) {
      downloadFailed(e, 'Download')
    }
    if (alive.current) setStarting(false)
  }

  async function jobAction(call: () => Promise<Job | null>) {
    setDownloadError(null)
    try {
      const next = await patiently(call)
      if (alive.current && next) setJob(next)
    } catch (e) {
      downloadFailed(e, 'Retry')
    }
  }

  // ---- the passkey ----------------------------------------------------------------------------

  async function createPasskey() {
    if (!secret || creating) return
    setCreating(true)
    setError(null)
    let code: string
    try {
      code = (await patiently(() => setupEnrollmentCode(secret))).code
    } catch (e) {
      if (!alive.current) return
      setCreating(false)
      if (isApiError(e) && e.status === 404) leave.current()
      else if (isApiError(e) && e.status === 403) setStage('ended')
      else setError(passkeyErrorMessage(e, 'Setup'))
      return
    }
    try {
      await enroll(code, guessDeviceName())
    } catch (e) {
      if (!alive.current) return
      setCreating(false)
      setError(passkeyErrorMessage(e, 'Setup'))
      return
    }
    if (!alive.current) return
    // Setup's server stops now: no more setup calls, and nothing of this run to come back to.
    setPasskeyMade(true)
    remember(STEP_KEY, null)
    remember(CHOICES_KEY, null)
    setStage('finishing')
    if (await waitForSetupDone()) {
      if (alive.current) go(DONE_STEP)
      return
    }
    if (alive.current) setStage('slow')
  }

  // ---- Done -----------------------------------------------------------------------------------

  function setUpLines(): string[] {
    const lines: string[] = []
    const finished = new Set(job?.items.filter((i) => i.state === 'done').map((i) => i.id))
    const items = catalog?.items ?? []
    const chat = catalog?.chat
    if (chat?.source === 'external') {
      lines.push(SETUP_COPY.doneExternal)
    } else {
      const downloaded = items.find((i) => i.kind === 'chat' && choices.chosen.includes(i.id) && finished.has(i.id))
      const inUse = chat?.source === 'local' ? items.find((i) => i.kind === 'chat' && i.card === chat.model) : undefined
      const model = downloaded ?? inUse
      if (model) lines.push(`Chat model: ${model.name}`)
    }
    for (const item of items) {
      if (item.kind === 'image' && choices.chosen.includes(item.id) && finished.has(item.id)) {
        lines.push(`Images: ${item.name}`)
      }
    }
    if (addons?.find((a) => a.id === 'analysis')?.ready) lines.push(SETUP_COPY.doneAnalysis)
    const startupAddon = addons?.find((a) => a.id === 'startup')
    if (choices.startup && startupAddon?.state !== 'unsupported') lines.push(SETUP_COPY.doneStartup)
    return lines
  }

  // The server's rule (`resume_record`): only a running job carries on after the hand-over,
  // with every item that wasn't stopped; the finished ones need nothing more shown here.
  const carryOn =
    job?.state === 'running' ? job.items.filter((i) => i.state !== 'cancelled' && i.state !== 'done') : []
  const stopped = job ? job.items.filter((i) => i.state === 'cancelled' || i.state === 'failed') : []

  function isImageKind(id: string) {
    const kind = kindOf(id)
    return kind === 'image' || kind === 'comfyui'
  }

  // ---- the page -------------------------------------------------------------------------------

  const focus = stepChanged.current
  // Not known until the add-ons are read: folder moves are offered only on Windows.
  const windowsHost = addons !== null && addons.find((a) => a.id === 'startup')?.state !== 'unsupported'

  function stepView() {
    switch (step) {
      case 0:
        return <Welcome key="welcome" catalog={catalog} focus={focus} onNext={() => go(1)} />
      case 1:
        return (
          <YourPc
            key="pc"
            info={pc}
            failed={pcFailed}
            onReload={() => void loadPc()}
            canMoveHere={windowsHost}
            moving={moving}
            onMove={move}
            focus={focus}
            onBack={() => go(0)}
            onNext={() => go(2)}
          />
        )
      case 2:
        return (
          <ChatModel
            key="chat"
            catalog={catalog}
            failed={catalogFailed}
            onReload={loadCatalog}
            chosen={choices.chosen}
            onChoose={choose}
            onUse={(item) => changeChat(() => modelsApi!.switchLocal(item.card), SETUP_COPY.chatLocal)}
            onExternal={(url, key, model) =>
              changeChat(() => modelsApi!.switchExternal(url, key, model), SETUP_COPY.chatExternal)
            }
            busy={chatBusy}
            error={chatError}
            notice={chatNotice}
            focus={focus}
            onBack={() => go(1)}
            onNext={() => go(3)}
          />
        )
      case 3:
        return (
          <AddOns
            key="addons"
            addons={addons}
            addonsFailed={addonsFailed}
            onReloadAddons={loadAddons}
            sandboxApi={sandboxApi!}
            catalog={catalog}
            chosen={choices.chosen}
            onChoose={choose}
            startup={choices.startup}
            startupBusy={startupBusy}
            startupError={startupError}
            onStartup={(enabled) => void changeStartup(enabled)}
            focus={focus}
            onBack={() => go(2)}
            onNext={() => go(4)}
          />
        )
      case 4:
        return (
          <Downloads
            key="downloads"
            job={job}
            plan={plan}
            makesDefault={makesDefault(catalog)}
            starting={starting}
            error={downloadError}
            onStart={() => void startDownloads()}
            onRetry={() => void jobAction(() => modelsApi!.retry())}
            onCancel={() => void jobAction(() => modelsApi!.cancel())}
            focus={focus}
            onBack={() => go(3)}
            onNext={() => go(PASSKEY_STEP)}
          />
        )
      case PASSKEY_STEP:
        return (
          <Passkey
            key="passkey"
            stage={stage === 'finishing' || stage === 'slow' ? stage : 'ready'}
            creating={creating}
            busyRetry={busyRetry}
            error={error}
            onCreate={() => void createPasskey()}
            focus={focus}
            onBack={() => go(4)}
          />
        )
      default:
        return (
          <Done
            key="done"
            setUp={setUpLines()}
            carryOn={carryOn.map((i) => i.name)}
            carryOnLine={carryOnLine(
              carryOn.some((i) => !isImageKind(i.id)),
              carryOn.some((i) => isImageKind(i.id)),
            )}
            stopped={stopped.map((i) => i.name)}
            onOpen={() => page.replace('/')}
            focus={focus}
          />
        )
    }
  }

  const wizard = stage === 'ready' || stage === 'finishing' || stage === 'slow'
  if (wizard) {
    return (
      <main className="setup">
        <div className="setup-wrap">
          <header className="setup-head">
            <span className="mark setup-mark" aria-hidden="true" />
            <h1 className="setup-title">Set up Hearthkeep</h1>
          </header>
          <Stepper steps={STEPS} current={step} label="Setup steps" />
          {step !== PASSKEY_STEP && busyRetry && (
            <p className="hint" role="status" aria-live="polite">
              {BUSY}
            </p>
          )}
          {stepView()}
        </div>
      </main>
    )
  }

  return (
    <main className="signin">
      <div className="signin-card">
        <span className={`mark signin-mark${stage === 'away' ? ' away' : ''}`} aria-hidden="true" />
        <h1 className="signin-title compact">Set up Hearthkeep</h1>
        {stage === 'no-secret' && (
          <p className="signin-lede">Open setup from this PC: double-click Setup.cmd in the Hearthkeep folder.</p>
        )}
        {stage === 'ended' && <p className="signin-lede">{ENDED}</p>}
        {stage === 'away' && (
          <>
            <p className="signin-lede">Can't reach Hearthkeep. Check that the setup window is still open on this PC.</p>
            <div className="signin-actions">
              <button type="button" className="btn primary lg block" onClick={() => setAttempt((n) => n + 1)}>
                Try again
              </button>
            </div>
          </>
        )}
        {stage === 'checking' && (
          <div className="signin-actions" aria-busy="true">
            <p className="signin-lede" role="status" aria-live="polite">
              {busyRetry ? BUSY : 'Checking the setup link…'}
            </p>
          </div>
        )}
      </div>
    </main>
  )
}
