import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { isApiError } from '../api/client'
import { getServer, page, restartServer, serverSession, waitForRestart, type ServerInfo, type WaitOptions } from '../api/server'
import { PASSKEY_NOT_CONFIRMED, passkeyErrorMessage, stepUp } from '../api/webauthn'
import { AlertIcon, InfoIcon } from '../shell/icons'

/** A plain sentence for anything a restart or a stop can throw (passkey or server). */
export function serverErrorMessage(error: unknown, action: 'Restart' | 'Stop'): string {
  if (isApiError(error, 'not_supervised')) {
    return "Hearthkeep can't restart itself here. Stop it, then open it from the Start menu."
  }
  if (isApiError(error, 'step_up_required')) return PASSKEY_NOT_CONFIRMED
  return passkeyErrorMessage(error, action)
}

/** What the server says about itself, loaded once. `failed` when it couldn't be asked. */
function useServerInfo(): { info: ServerInfo | null; failed: boolean } {
  const [info, setInfo] = useState<ServerInfo | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let live = true
    getServer()
      .then((i) => live && setInfo(i))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [])
  return { info, failed }
}

/** Where a restart was asked for, so only that place shows its progress and errors. */
export type RestartPlace = 'addons' | 'general' | 'models'

export type Restart = {
  restarting: boolean
  error: string | null
  place: RestartPlace | null
  restart(place: RestartPlace): Promise<void>
}

/** Restart with the passkey, wait for the server to come back, then load the app afresh. */
function useRestart(info: ServerInfo | null, wait?: WaitOptions): Restart {
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [place, setPlace] = useState<RestartPlace | null>(null)
  const alive = useRef(true)
  const busy = useRef(false)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const restart = useCallback(async (from: RestartPlace) => {
    if (!info || busy.current) return
    busy.current = true
    setPlace(from)
    setRestarting(true)
    setError(null)
    try {
      const token = await stepUp('server')
      await restartServer(token)
      if (await waitForRestart(info.started_at, wait)) {
        page.reload()
        return
      }
      if (alive.current) setError("Hearthkeep didn't come back. Open it from the Start menu on the PC.")
    } catch (e) {
      if (alive.current) setError(serverErrorMessage(e, 'Restart'))
    } finally {
      busy.current = false
    }
    if (alive.current) setRestarting(false)
  }, [info, wait])

  return useMemo(() => ({ restarting, error, place, restart }), [restarting, error, place, restart])
}

export type ServerState = {
  info: ServerInfo | null
  failed: boolean
  restart: Restart
  /** Stopped from this page; stays so for the page's life. */
  stopped: boolean
  markStopped(): void
  /** Something on this page changed what needs a restart (a model download or switch). */
  noteRestartNeeded(): void
}

const ServerContext = createContext<ServerState | null>(null)

/** One ask of the server and one restart for everything in Settings that shows them. */
export function ServerProvider({ wait, children }: { wait?: WaitOptions; children: ReactNode }) {
  const { info: asked, failed } = useServerInfo()
  const restart = useRestart(asked, wait)
  const [noted, setNoted] = useState(false)
  const noteRestartNeeded = useCallback(() => setNoted(true), [])
  // The server was asked once; a change made since then needs a restart too.
  const info = useMemo(
    () => (asked && noted && !asked.restart_needed ? { ...asked, restart_needed: true } : asked),
    [asked, noted],
  )
  const [stopped, setStopped] = useState(serverSession.stopped)
  const markStopped = useCallback(() => {
    serverSession.stopped = true
    setStopped(true)
  }, [])
  const value = useMemo(
    () => ({ info, failed, restart, stopped, markStopped, noteRestartNeeded }),
    [info, failed, restart, stopped, markStopped, noteRestartNeeded],
  )
  return <ServerContext.Provider value={value}>{children}</ServerContext.Provider>
}

/** The shared server state, or null outside a `ServerProvider`. */
export function useServer(): ServerState | null {
  return useContext(ServerContext)
}

/** "A change needs Hearthkeep to restart", with a button, only when one is needed and possible. */
export function RestartNotice({ place }: { place: RestartPlace }) {
  const server = useServer()
  if (!server || server.stopped) return null
  const { info, restart } = server
  if (!info?.restart_needed || !info.supervised) return null
  return (
    <div className="restart-notice">
      <div className="note info">
        <InfoIcon />
        <span>A change needs Hearthkeep to restart.</span>
      </div>
      <div className="settings-actions">
        <button
          type="button"
          className="btn"
          aria-busy={restart.restarting || undefined}
          onClick={() => {
            if (!restart.restarting) void restart.restart(place)
          }}
        >
          Restart now
        </button>
      </div>
      {place !== 'general' && <RestartStatus restart={restart} place={place} />}
    </div>
  )
}

/** "Restarting…" for screen readers too, and why a restart didn't work, for a restart asked for
 *  at `place`. */
export function RestartStatus({ restart, place }: { restart: Restart; place: RestartPlace }) {
  const here = restart.place === place
  return (
    <>
      <p className="settings-lede" role="status" aria-live="polite">
        {here && restart.restarting ? 'Restarting Hearthkeep…' : ''}
      </p>
      {here && restart.error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{restart.error}</span>
        </div>
      )}
    </>
  )
}
