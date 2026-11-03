import { useCallback, useEffect, useId, useState } from 'react'
import { api } from '../api/client'
import { AddonsPanel } from '../components/AddonsPanel'
import { DevicesPanel } from '../components/DevicesPanel'
import { FoldersPanel } from '../components/FoldersPanel'
import { GitHubPanel } from '../components/GitHubPanel'
import { NotificationToggle } from '../components/NotificationToggle'
import { useAddonsState, type SettingsAnchor } from '../shell/AddonsContext'
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
    <section id="settings-startup" className="settings-section" aria-labelledby={titleId} tabIndex={-1}>
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

type Props = {
  /** A section or add-on card to scroll to and focus once it is on screen. */
  focus?: SettingsAnchor | null
  /** Called after the focus moved, so the anchor can be cleared and a later visit doesn't jump. */
  onFocused?: () => void
}

export function Settings({ focus, onFocused }: Props = {}) {
  const notifyId = useId()
  // Inside the app shell its anchors arrive as `focus`; on its own, Settings moves itself.
  const inShell = useAddonsState() !== null
  const [own, setOwn] = useState<SettingsAnchor | null>(null)
  const [addonsLoaded, setAddonsLoaded] = useState(false)
  const onLoaded = useCallback(() => setAddonsLoaded(true), [])
  const target = focus ?? own

  useEffect(() => {
    if (!target) return
    const el = document.getElementById(target)
    // An add-on's card exists only once the add-ons are loaded; wait for them.
    if (!el && target.startsWith('addon-') && !addonsLoaded) return
    if (el) {
      el.scrollIntoView?.({ block: 'start' })
      el.focus({ preventScroll: true })
    }
    setOwn(null)
    onFocused?.()
  }, [target, addonsLoaded, onFocused])

  return (
    <div className="settings">
      <AddonsPanel onNavigate={inShell ? undefined : setOwn} onLoaded={onLoaded} />
      <DevicesPanel />
      <FoldersPanel />
      <GitHubPanel />
      <section className="settings-section" aria-labelledby={notifyId}>
        <h2 id={notifyId}>Notifications</h2>
        <NotificationToggle />
      </section>
      <StartupPanel />
    </div>
  )
}
