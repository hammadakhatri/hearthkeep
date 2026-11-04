import { useCallback, useEffect, useId, useState } from 'react'
import { api, isApiError, post } from '../api/client'
import { sessionModelsApi } from '../api/models'
import { PASSKEY_NOT_CONFIRMED, passkeyErrorMessage, stepUp } from '../api/webauthn'
import { AddonsPanel } from '../components/AddonsPanel'
import { DevicesPanel } from '../components/DevicesPanel'
import { FoldersPanel } from '../components/FoldersPanel'
import { GitHubPanel } from '../components/GitHubPanel'
import { ModelsPanel } from '../components/ModelsPanel'
import { NotificationToggle } from '../components/NotificationToggle'
import { ServerProvider } from '../components/RestartNotice'
import { ServerPanel } from '../components/ServerPanel'
import { StartupSwitch, startupFailedMessage, type StartupState } from '../components/StartupSwitch'
import { useAddonsState, type SettingsAnchor } from '../shell/AddonsContext'
import { AlertIcon } from '../shell/icons'

function startupError(error: unknown): string {
  if (isApiError(error, 'step_up_required')) return PASSKEY_NOT_CONFIRMED
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  if (isApiError(error)) return startupFailedMessage(error)
  return passkeyErrorMessage(error, 'Turning on start at login')
}

/** Start at login: turning it on takes a passkey, turning it off does not. */
function StartupPanel() {
  const titleId = useId()
  const [state, setState] = useState<StartupState | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api<StartupState>('/api/settings/startup')
      .then((s) => live && setState(s))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [])

  async function change(enabled: boolean) {
    setBusy(true)
    setError(null)
    try {
      const body = enabled ? { enabled, step_up_token: await stepUp('settings') } : { enabled }
      setState(await api<StartupState>('/api/settings/startup', post(body)))
    } catch (e) {
      setError(startupError(e))
    }
    setBusy(false)
  }

  return (
    <section id="settings-startup" className="settings-section" aria-labelledby={titleId} tabIndex={-1}>
      <h2 id={titleId}>Start at login</h2>
      <div className="tile startup" aria-busy={(state === null && !failed) || undefined}>
        {failed ? (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>Couldn't check start at login.</span>
          </div>
        ) : (
          <StartupSwitch state={state} onChange={change} busy={busy} error={error} />
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
    // One ask of the server for the restart notice in Add-ons, the models and the General section.
    <ServerProvider>
      <div className="settings">
        <AddonsPanel onNavigate={inShell ? undefined : setOwn} onLoaded={onLoaded} />
        <ModelsPanel api={sessionModelsApi} />
        <DevicesPanel />
        <FoldersPanel />
        <GitHubPanel />
        <section className="settings-section" aria-labelledby={notifyId}>
          <h2 id={notifyId}>Notifications</h2>
          <NotificationToggle />
        </section>
        <StartupPanel />
        <ServerPanel />
      </div>
    </ServerProvider>
  )
}
