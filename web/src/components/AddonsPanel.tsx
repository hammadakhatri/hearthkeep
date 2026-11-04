import { useEffect, useId, useRef, useState } from 'react'
import { sessionSandboxApi, useAddons, type Addon, type AddonsState } from '../api/addons'
import { actionTarget, useAddonsState, useOpenSettings, type SettingsAnchor } from '../shell/AddonsContext'
import { AlertIcon } from '../shell/icons'
import { RestartNotice } from './RestartNotice'
import { SandboxBuild } from './SandboxBuild'

type Badge = { label: string; green?: boolean }

function badgeFor(addon: Addon): Badge {
  switch (addon.state) {
    case 'ready':
      return { label: 'Ready', green: true }
    case 'on':
      return { label: 'On', green: true }
    case 'off':
      return { label: 'Off' }
    case 'unsupported':
      return { label: 'Only on Windows' }
    case 'this_pc_only':
      return { label: 'This PC only' }
    case 'starting':
      return { label: 'Loading' }
    case 'offline':
      return { label: 'Offline' }
    case 'error':
      return { label: 'Needs attention' }
    default:
      return { label: 'Not set up' }
  }
}

/** The one button an add-on's card gets, when its fix leads to a screen that acts. */
function actionLabel(addon: Addon): string | null {
  if (addon.id === 'chat' && addon.action === 'open_setup') return 'Choose a chat model'
  if (addon.id === 'images' && addon.action === 'install') return 'Add image models'
  if (addon.id === 'images' && addon.action === 'open_setup') return 'Check image models'
  if (addon.action === 'add_token') return 'Add a GitHub token'
  if (addon.action === 'turn_on') return 'Start at login settings'
  return null
}

/** What the add-on check knows about Docker, for the sandbox build on the Analysis card. */
function dockerHint(addon: Addon): 'missing' | 'stopped' | 'ready' | null {
  if (addon.id !== 'analysis' || addon.ready) return null
  if (addon.action === 'install') return 'missing'
  if (addon.action === 'start_docker') return 'stopped'
  if (addon.action === 'build_image') return 'ready'
  return null
}

type Props = {
  /** Where a button sends the owner; the app shell's own when it provides one. */
  onNavigate?: (anchor: SettingsAnchor) => void
  /** Called once loading has settled (the add-ons are on screen, or it failed), so a card can be focused or given up on. */
  onLoaded?: () => void
}

/** Settings › Add-ons: what can be used on this PC, what each add-on needs, and "Check again". */
export function AddonsPanel(props: Props) {
  const shared = useAddonsState()
  // Without the app shell (Settings on its own) the panel loads the list itself.
  return shared ? <AddonsView state={shared} {...props} /> : <OwnAddons {...props} />
}

function OwnAddons(props: Props) {
  return <AddonsView state={useAddons()} {...props} />
}

function AddonsView({ state, onNavigate, onLoaded }: Props & { state: AddonsState }) {
  const titleId = useId()
  const openSettings = useOpenSettings()
  const go = onNavigate ?? openSettings
  const { addons, failed, refreshing } = state

  useEffect(() => {
    if (addons || failed) onLoaded?.()
  }, [addons, failed, onLoaded])

  // "Checked just now" after a Check again that worked, for screen readers too.
  const wasRefreshing = useRef(false)
  const [checked, setChecked] = useState(false)
  useEffect(() => {
    if (refreshing) setChecked(false)
    else if (wasRefreshing.current && !failed) setChecked(true)
    wasRefreshing.current = refreshing
  }, [refreshing, failed])

  return (
    <section id="settings-addons" className="settings-section" aria-labelledby={titleId} tabIndex={-1}>
      <div className="addons-head">
        <h2 id={titleId}>Add-ons</h2>
        <button
          type="button"
          className="btn ghost"
          aria-busy={refreshing || undefined}
          onClick={() => {
            if (!refreshing) void state.refresh()
          }}
        >
          Check again
        </button>
      </div>
      <RestartNotice place="addons" />
      <p className="settings-lede">What Hearthkeep can use on this PC, and what each part still needs.</p>
      <p className="settings-lede" role="status" aria-live="polite">
        {checked ? 'Checked just now' : ''}
      </p>
      {failed && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>Couldn't check the add-ons. Try again.</span>
        </div>
      )}
      {addons === null ? (
        !failed && (
          <div className="device-list skeleton" role="status" aria-busy="true" aria-label="Loading add-ons">
            <span className="bar w30" />
            <span className="bar w60" />
          </div>
        )
      ) : (
        <div className="addons-grid">
          {addons.map((addon) => {
            const badge = badgeFor(addon)
            const label = actionLabel(addon)
            // Only once Analysis works: the folders it reads don't fix anything else.
            const folders = addon.id === 'analysis' && addon.ready
            const docker = dockerHint(addon)
            return (
              <article key={addon.id} id={`addon-${addon.id}`} className="tile addon" tabIndex={-1}>
                <div className="addon-top">
                  <h3>{addon.name}</h3>
                  <span className={`badge${badge.green ? ' green' : ''}`}>{badge.label}</span>
                </div>
                <p className="addon-about">{addon.about}</p>
                <p className="addon-needs">Needs: {addon.requires}</p>
                {!addon.ready && <p className="addon-message">{addon.message}</p>}
                {docker && <SandboxBuild api={sessionSandboxApi} docker={docker} onFinished={state.reload} />}
                {(label || folders) && (
                  <div className="settings-actions">
                    {label && (
                      <button type="button" className="btn" onClick={() => go(actionTarget(addon))}>
                        {label}
                      </button>
                    )}
                    {folders && (
                      <button type="button" className="btn ghost" onClick={() => go('settings-folders')}>
                        Shared folders
                      </button>
                    )}
                  </div>
                )}
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
