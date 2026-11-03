import type { Addon } from '../api/addons'
import { actionTarget, useOpenSettings } from '../shell/AddonsContext'

/** The one panel any view shows when its add-on isn't ready: what it is, what it needs, and the way to set it up. */
export function SetUpPanel({ addon }: { addon: Addon }) {
  const openSettings = useOpenSettings()
  return (
    <section className="setup-panel" aria-label={`Set up ${addon.name}`}>
      <h3 className="setup-panel-title">{addon.about}</h3>
      <p className="setup-panel-needs">Needs: {addon.requires}</p>
      <p className="setup-panel-message">{addon.message}</p>
      <button type="button" className="btn primary" onClick={() => openSettings(actionTarget(addon))}>
        Set up {addon.name}
      </button>
    </section>
  )
}
