import { useCallback, useEffect, useRef, useState } from 'react'
import type { SandboxBuildApi, SandboxBuildState } from '../api/addons'
import { isApiError } from '../api/client'
import { AlertIcon, CheckIcon } from '../shell/icons'

export const DOCKER_PAGE = 'https://www.docker.com/products/docker-desktop/'
const POLL_MS = 2000
const IDLE: SandboxBuildState = { state: 'idle', step: null, reason: null }

type Props = {
  /** Settings passes the session adapter (a passkey inside `startBuild`); the wizard its own. */
  api: SandboxBuildApi
  /** What the add-on check already knows about Docker, before any build is tried. */
  docker?: 'missing' | 'stopped' | 'ready'
  /** The caller looks at Docker again (the wizard, which has no other "Check again"). */
  onCheckAgain?: () => void
  /** The build ended, well or not: the caller can read the add-ons again. */
  onFinished?: () => void
  /** Whether a build is running, so a card can keep this on screen whatever the list says. */
  onBusyChange?: (busy: boolean) => void
}

/** Building the analysis sandbox: Docker missing or stopped, the Build button, progress, done
 *  or failed with Retry. Shows state words only; what Docker prints stays on the PC. */
export function SandboxBuild({ api, docker = 'ready', onCheckAgain, onFinished, onBusyChange }: Props) {
  const [build, setBuild] = useState<SandboxBuildState>(IDLE)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState(false)
  const alive = useRef(true)
  const apiRef = useRef(api)
  apiRef.current = api
  const finishedRef = useRef(onFinished)
  finishedRef.current = onFinished

  // A build that is already running (the page was reloaded) is picked up; an old result is not.
  useEffect(() => {
    alive.current = true
    apiRef.current
      .getBuild()
      .then((b) => alive.current && b.state === 'building' && setBuild(b))
      .catch(() => {})
    return () => {
      alive.current = false
    }
  }, [])

  const building = build.state === 'building'

  // Every 2 s while building; nothing while the page is hidden, and a look as it comes back.
  useEffect(() => {
    if (!building) return
    const poll = () => {
      if (document.visibilityState === 'hidden') return
      apiRef.current
        .getBuild()
        .then((b) => alive.current && setBuild(b))
        .catch(() => {})
    }
    const timer = window.setInterval(poll, POLL_MS)
    document.addEventListener('visibilitychange', poll)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', poll)
    }
  }, [building])

  const busyRef = useRef(onBusyChange)
  busyRef.current = onBusyChange
  useEffect(() => {
    busyRef.current?.(building)
  }, [building])
  useEffect(() => () => busyRef.current?.(false), [])

  const wasBuilding = useRef(false)
  useEffect(() => {
    if (wasBuilding.current && (build.state === 'done' || build.state === 'failed')) finishedRef.current?.()
    wasBuilding.current = building
  }, [build.state, building])

  const start = useCallback(async () => {
    setStarting(true)
    setError(false)
    try {
      const next = await apiRef.current.startBuild()
      if (alive.current) setBuild(next)
      if (next.state === 'done' || next.state === 'failed') finishedRef.current?.()
    } catch (e) {
      if (!alive.current) return
      if (isApiError(e, 'busy')) setBuild({ state: 'building', step: null, reason: null })
      else setError(true)
    } finally {
      if (alive.current) setStarting(false)
    }
  }, [])

  const tryAgain = () => {
    setBuild(IDLE)
    setError(false)
    onCheckAgain?.()
  }

  if (building) {
    const step = build.step === 'repo' ? 2 : 1
    return (
      <div className="sandbox-build" aria-busy="true">
        <p className="addon-message" role="status">
          Building the sandbox, step {step} of 2.
        </p>
        <p className="perm-sub">This takes a few minutes the first time. You can leave this page open.</p>
      </div>
    )
  }

  if (build.state === 'done') {
    return (
      <div className="sandbox-build">
        <p className="addon-message" role="status">
          <CheckIcon aria-hidden="true" /> The sandbox is ready.
        </p>
      </div>
    )
  }

  const missing = build.state === 'failed' ? build.reason === 'docker_missing' : docker === 'missing'
  const stopped = build.state === 'failed' ? build.reason === 'docker_stopped' : docker === 'stopped'
  const buildFailed = build.state === 'failed' && build.reason === 'build_failed'

  if (missing) {
    return (
      <div className="sandbox-build">
        <p className="perm-sub">Install Docker Desktop and start it, then Check again.</p>
        <div className="settings-actions">
          <a className="btn" href={DOCKER_PAGE} target="_blank" rel="noopener noreferrer">
            Get Docker Desktop
          </a>
          {(onCheckAgain || build.state === 'failed') && (
            <button type="button" className="btn ghost" onClick={tryAgain}>
              {onCheckAgain ? 'Check again' : 'Try again'}
            </button>
          )}
        </div>
      </div>
    )
  }

  if (stopped) {
    // In the add-ons list the card's own message says this; only a build that found it out, or
    // the wizard (which has no other Check again), adds the line and the button.
    if (build.state !== 'failed' && !onCheckAgain) return null
    return (
      <div className="sandbox-build">
        <p className="perm-sub">Start Docker Desktop, then Check again.</p>
        <div className="settings-actions">
          <button type="button" className="btn ghost" onClick={tryAgain}>
            {onCheckAgain ? 'Check again' : 'Try again'}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="sandbox-build">
      {buildFailed && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>The sandbox didn't build. Check that Docker Desktop is running and this PC is online, then try again.</span>
        </div>
      )}
      {error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>Couldn't start the build. Try again.</span>
        </div>
      )}
      {!buildFailed && <p className="perm-sub">This takes a few minutes the first time.</p>}
      <div className="settings-actions">
        <button type="button" className="btn" aria-busy={starting || undefined} disabled={starting} onClick={() => void start()}>
          {buildFailed ? 'Retry' : 'Build the sandbox'}
        </button>
      </div>
    </div>
  )
}
