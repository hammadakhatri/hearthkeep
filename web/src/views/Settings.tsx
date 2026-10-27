import { useEffect, useId, useState } from 'react'
import { api } from '../api/client'
import { DevicesPanel } from '../components/DevicesPanel'
import { FoldersPanel } from '../components/FoldersPanel'
import { NotificationToggle } from '../components/NotificationToggle'
import { AlertIcon, CopyIcon } from '../shell/icons'

type Startup = {
  supported: boolean
  enabled: boolean
  /** On, but the task starts Hearthkeep with another data folder. */
  other_data_folder?: boolean
  command: string | null
}

/** Start at login is changed on the PC itself; this only shows the state and the command. */
function StartupPanel() {
  const titleId = useId()
  const [state, setState] = useState<Startup | null>(null)
  const [failed, setFailed] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let live = true
    api<Startup>('/api/settings/startup')
      .then((s) => live && setState(s))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [])

  async function copy(command: string) {
    try {
      await navigator.clipboard.writeText(command)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  const label =
    state === null
      ? ''
      : !state.supported
        ? 'Only on Windows'
        : state.enabled && state.other_data_folder
          ? 'On, for another data folder'
          : state.enabled
            ? 'On'
            : 'Off'
  return (
    <section className="settings-section" aria-labelledby={titleId}>
      <h2 id={titleId}>Start at login</h2>
      <div className="tile startup" aria-busy={(state === null && !failed) || undefined}>
        <div className="startup-row">
          <p className="perm-sub">Hearthkeep starts on its own, with no window, when you sign in to the PC.</p>
          {state && <span className={`badge${state.enabled && !state.other_data_folder ? ' green' : ''}`}>{label}</span>}
        </div>
        {failed && (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>Couldn't check start at login.</span>
          </div>
        )}
        {state?.command && (
          <>
            <p className="startup-how">To change it, run this on the PC:</p>
            <div className="command">
              <code>{state.command}</code>
              <button
                type="button"
                className="btn ghost"
                aria-label="Copy command"
                onClick={() => copy(state.command as string)}
              >
                <CopyIcon />
                <span aria-live="polite">{copied ? 'Copied' : 'Copy'}</span>
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  )
}

export function Settings() {
  const notifyId = useId()
  return (
    <div className="settings">
      <DevicesPanel />
      <FoldersPanel />
      <section className="settings-section" aria-labelledby={notifyId}>
        <h2 id={notifyId}>Notifications</h2>
        <NotificationToggle />
      </section>
      <StartupPanel />
    </div>
  )
}
