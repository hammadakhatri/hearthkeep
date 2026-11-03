import { useCallback, useEffect, useRef, useState } from 'react'
import { isApiError } from '../api/client'
import { getServer, page, restartServer, waitForRestart, type ServerInfo } from '../api/server'
import { passkeyErrorMessage, stepUp } from '../api/webauthn'
import { AlertIcon, InfoIcon } from '../shell/icons'

/** A plain sentence for anything a restart or a stop can throw (passkey or server). */
export function serverErrorMessage(error: unknown, action: 'Restart' | 'Stop'): string {
  if (isApiError(error, 'not_supervised')) {
    return "Hearthkeep can't restart itself here. Stop it, then open it from the Start menu."
  }
  if (isApiError(error, 'step_up_required')) return "Your passkey couldn't be confirmed. Try again."
  return passkeyErrorMessage(error, action)
}

/** What the server says about itself, loaded once. `failed` when it couldn't be asked. */
export function useServerInfo(): { info: ServerInfo | null; failed: boolean } {
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

export type Restart = { restarting: boolean; error: string | null; restart(): Promise<void> }

/** Restart with the passkey, wait for the server to come back, then load the app afresh. */
export function useRestart(info: ServerInfo | null): Restart {
  const [restarting, setRestarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)
  const busy = useRef(false)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const restart = useCallback(async () => {
    if (!info || busy.current) return
    busy.current = true
    setRestarting(true)
    setError(null)
    try {
      const token = await stepUp('server')
      await restartServer(token)
      if (await waitForRestart(info.started_at)) {
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
  }, [info])

  return { restarting, error, restart }
}

type Props = {
  /** The server's state when the page already has it; otherwise the notice asks for it. */
  info?: ServerInfo | null
  /** A restart the page shares (and shows the errors of); otherwise the notice has its own. */
  restart?: Restart
}

/** "A change needs Hearthkeep to restart", with a button, only when one is needed and possible. */
export function RestartNotice({ info, restart }: Props) {
  return info === undefined ? <OwnNotice /> : <NoticeWith info={info} restart={restart} />
}

function OwnNotice() {
  return <NoticeWith info={useServerInfo().info} />
}

function NoticeWith({ info, restart }: { info: ServerInfo | null; restart?: Restart }) {
  const own = useRestart(info)
  const action = restart ?? own
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
          aria-busy={action.restarting || undefined}
          onClick={() => {
            if (!action.restarting) void action.restart()
          }}
        >
          Restart now
        </button>
      </div>
      {!restart && <RestartStatus restart={own} />}
    </div>
  )
}

/** "Restarting…" for screen readers too, and why a restart didn't work. */
export function RestartStatus({ restart }: { restart: Restart }) {
  return (
    <>
      <p className="settings-lede" role="status" aria-live="polite">
        {restart.restarting ? 'Restarting Hearthkeep…' : ''}
      </p>
      {restart.error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{restart.error}</span>
        </div>
      )}
    </>
  )
}
