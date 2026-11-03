import { useId, useState } from 'react'
import { stopServer } from '../api/server'
import { stepUp } from '../api/webauthn'
import { AlertIcon, InfoIcon } from '../shell/icons'
import { ConfirmDelete } from './ConfirmDelete'
import { RestartNotice, RestartStatus, serverErrorMessage, useRestart, useServerInfo } from './RestartNotice'

/** Settings › General: the version, and restarting or stopping Hearthkeep (each with the passkey). */
export function ServerPanel() {
  const titleId = useId()
  const { info, failed } = useServerInfo()
  const restart = useRestart(info)
  const [asking, setAsking] = useState(false)
  const [stopped, setStopped] = useState(false)

  async function stop() {
    const token = await stepUp('server')
    await stopServer(token)
    setAsking(false)
    setStopped(true)
  }

  return (
    <section id="settings-general" className="settings-section" aria-labelledby={titleId} tabIndex={-1}>
      <h2 id={titleId}>General</h2>
      {stopped ? (
        <div className="note info" role="status">
          <InfoIcon />
          <span>Hearthkeep has stopped. Open it again from the Start menu on the PC.</span>
        </div>
      ) : (
        <div className="tile server" aria-busy={(info === null && !failed) || undefined}>
          {info && <p className="perm-sub">Version {info.version}</p>}
          {failed && (
            <div className="note error" role="alert">
              <AlertIcon />
              <span>Couldn't check Hearthkeep's version.</span>
            </div>
          )}
          <RestartNotice info={info} restart={restart} />
          <div className="settings-actions">
            {info?.supervised && (
              <button
                type="button"
                className="btn"
                aria-busy={restart.restarting || undefined}
                onClick={() => {
                  if (!restart.restarting) void restart.restart()
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
          <RestartStatus restart={restart} />
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
