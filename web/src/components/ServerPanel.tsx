import { useEffect, useId, useRef, useState } from 'react'
import { stopServer } from '../api/server'
import { stepUp } from '../api/webauthn'
import { AlertIcon, InfoIcon } from '../shell/icons'
import { ConfirmDelete } from './ConfirmDelete'
import { RestartNotice, RestartStatus, ServerProvider, serverErrorMessage, useServer, type ServerState } from './RestartNotice'

const STOPPED = 'Hearthkeep has stopped. Open it again from the Start menu on the PC.'

/** Settings › General: the version, and restarting or stopping Hearthkeep (each with the passkey).
 *  Shares Settings' `ServerProvider`; on its own it brings one. */
export function ServerPanel() {
  const server = useServer()
  if (server) return <GeneralSection server={server} />
  return (
    <ServerProvider>
      <ServerPanel />
    </ServerProvider>
  )
}

function GeneralSection({ server }: { server: ServerState }) {
  const titleId = useId()
  const { info, failed, restart, stopped, markStopped } = server
  const [asking, setAsking] = useState(false)
  const section = useRef<HTMLElement>(null)
  const justStopped = useRef(false)

  // After a stop the buttons are gone: the focus goes to the section that says what happened.
  useEffect(() => {
    if (!stopped || !justStopped.current) return
    justStopped.current = false
    section.current?.focus()
  }, [stopped])

  async function stop() {
    const token = await stepUp('server')
    await stopServer(token)
    justStopped.current = true
    setAsking(false)
    markStopped()
  }

  return (
    <section
      ref={section}
      id="settings-general"
      className="settings-section"
      aria-labelledby={titleId}
      tabIndex={-1}
    >
      <h2 id={titleId}>General</h2>
      {/* Always on the page, so the stop is announced when the text arrives. */}
      <div role="status" aria-live="polite">
        {stopped && (
          <div className="note info">
            <InfoIcon />
            <span>{STOPPED}</span>
          </div>
        )}
      </div>
      {!stopped && (
        <div className="tile server" aria-busy={(info === null && !failed) || undefined}>
          {info && <p className="perm-sub">Version {info.version}</p>}
          {failed && (
            <div className="note error" role="alert">
              <AlertIcon />
              <span>Couldn't check Hearthkeep's version.</span>
            </div>
          )}
          <RestartNotice place="general" />
          <div className="settings-actions">
            {info?.supervised && (
              <button
                type="button"
                className="btn"
                aria-busy={restart.restarting || undefined}
                onClick={() => {
                  if (!restart.restarting) void restart.restart('general')
                }}
              >
                Restart Hearthkeep
              </button>
            )}
            <button
              type="button"
              className="btn danger"
              disabled={restart.restarting}
              onClick={() => setAsking(true)}
            >
              Stop Hearthkeep
            </button>
          </div>
          <RestartStatus restart={restart} place="general" />
        </div>
      )}
      {asking && (
        <ConfirmDelete
          title="Stop Hearthkeep?"
          body="It stops on this PC and on your other devices until you open it again from the Start menu on the PC."
          confirmLabel="Stop"
          errorMessage={(e) => serverErrorMessage(e, 'Stop')}
          onConfirm={stop}
          onCancel={() => setAsking(false)}
        />
      )}
    </section>
  )
}
