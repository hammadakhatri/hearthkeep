import { useCallback, useEffect, useState } from 'react'
import { api, isApiError } from '../api/client'
import { passkeyErrorMessage, stepUp } from '../api/webauthn'
import { ModelList } from '../components/ModelList'
import { TriToggle, type Level } from '../components/TriToggle'
import { AlertIcon, InfoIcon } from '../shell/icons'

export type ToolSetting = {
  tool: string
  level: Level
  effective_level: Level
  description: string
  tools?: string[]
}

/** Owner-facing names for the permission keys the server knows. */
const NAMES: Record<string, string> = {
  wallet: 'Wallet',
  news: 'News & web search',
  files: 'Files',
  python: 'Python',
  images: 'Image generation',
  github: 'GitHub',
  demo: 'Demo tools',
}

export function toolName(key: string): string {
  return NAMES[key] ?? key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, ' ')
}

const WIDTH: Record<Level, number> = { off: 0, ask: 1, allowed: 2 }
const putLevel = (tool: string, body: { level: Level; step_up_token?: string }) =>
  api<ToolSetting>(`/api/settings/tools/${encodeURIComponent(tool)}`, { method: 'PUT', body: JSON.stringify(body) })

function ToolRow({ setting, onSaved }: { setting: ToolSetting; onSaved: (s: ToolSetting) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const name = toolName(setting.tool)
  const installed = setting.tools === undefined || setting.tools.length > 0

  async function change(level: Level) {
    setBusy(true)
    setError(null)
    try {
      // Loosening needs Face ID / Touch ID; tightening never does.
      const widening = WIDTH[level] > WIDTH[setting.level]
      let saved: ToolSetting
      try {
        saved = await putLevel(setting.tool, widening ? { level, step_up_token: await stepUp('settings') } : { level })
      } catch (e) {
        if (widening || !isApiError(e, 'step_up_required')) throw e
        // The level changed elsewhere since this page loaded, so this is a widening after all.
        saved = await putLevel(setting.tool, { level, step_up_token: await stepUp('settings') })
      }
      onSaved(saved)
    } catch (e) {
      if (isApiError(e, 'step_up_required')) setError("Face ID / Touch ID couldn't confirm it. Try again.")
      else if (isApiError(e, 'network')) setError("Can't reach Hearthkeep. Try again in a moment.")
      else if (isApiError(e)) setError("That change didn't save. Try again.")
      else setError(passkeyErrorMessage(e, 'Change'))
    }
    setBusy(false)
  }

  return (
    <li className="perm" aria-busy={busy || undefined}>
      <div className="perm-text">
        <b>{name}</b>
        <p className="perm-sub">{installed ? setting.description : 'Arrives in a later update. You can choose its level now.'}</p>
        {setting.effective_level !== setting.level && (
          <p className="perm-flag">
            <InfoIcon />
            <span>Needs Docker to run freely</span>
          </p>
        )}
        {error && (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>{error}</span>
          </div>
        )}
      </div>
      <TriToggle label={name} value={setting.level} onChange={change} disabled={busy} />
    </li>
  )
}

export function ToolsPermissions() {
  const [settings, setSettings] = useState<ToolSetting[] | null>(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(() => {
    setFailed(false)
    api<ToolSetting[]>('/api/settings/tools')
      .then(setSettings)
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  function saved(next: ToolSetting) {
    setSettings((all) => all?.map((s) => (s.tool === next.tool ? { ...s, ...next } : s)) ?? null)
  }

  return (
    <section className="perms" aria-labelledby="perms-title">
      <header className="perms-head">
        <h2 id="perms-title">What your assistant may do</h2>
        <p>
          <b>Allowed</b> runs on its own, <b>Ask</b> waits for you, <b>Off</b> is never offered. Anything that leaves
          your computer always asks first. Loosening a tool asks for Face ID / Touch ID.
        </p>
      </header>
      <ModelList />
      <h3 id="tools-title" className="perms-sub-head">
        Tools
      </h3>
      {failed ? (
        <div className="note error perms-error" role="alert">
          <AlertIcon />
          <span>Couldn't load your tools.</span>
          <button type="button" className="btn ghost" onClick={load}>
            Retry
          </button>
        </div>
      ) : settings === null ? (
        <ul className="perm-list" aria-busy="true" aria-label="Loading tools">
          {[0, 1, 2].map((i) => (
            <li key={i} className="perm skeleton" aria-hidden="true">
              <div className="perm-text">
                <span className="bar w30" />
                <span className="bar w60" />
              </div>
              <span className="bar tri-ghost" />
            </li>
          ))}
        </ul>
      ) : (
        <ul className="perm-list">
          {settings.map((setting) => (
            <ToolRow key={setting.tool} setting={setting} onSaved={saved} />
          ))}
        </ul>
      )}
    </section>
  )
}
