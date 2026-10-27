import { useCallback, useEffect, useId, useState, type FormEvent } from 'react'
import { api, isApiError, post } from '../api/client'
import { passkeyErrorMessage, stepUp } from '../api/webauthn'
import { AlertIcon, FilesIcon, PasskeyIcon } from '../shell/icons'

type Folder = { id: string; label: string; path: string }
type Suggestion = { label: string; path: string }
type Folders = { folders: Folder[]; suggestions: Suggestion[] }

function failure(error: unknown, action: string): string {
  if (isApiError(error, 'folder_refused')) {
    const message = (error.body as { message?: unknown } | null)?.message
    if (typeof message === 'string' && message) return message
    return "Hearthkeep can't use that folder."
  }
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  if (isApiError(error, 'step_up_required')) return "Your passkey couldn't be confirmed. Try again."
  if (isApiError(error)) return `${action} didn't work. Try again.`
  return passkeyErrorMessage(error, action)
}

/** The folders on this PC Hearthkeep may search and send from. Adding one asks for a passkey. */
export function FoldersPanel() {
  const [data, setData] = useState<Folders | null>(null)
  const [failed, setFailed] = useState(false)
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const titleId = useId()
  const pathHint = useId()

  const load = useCallback(() => {
    setFailed(false)
    api<Folders>('/api/folders')
      .then(setData)
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  async function add(folderPath: string) {
    setBusy(true)
    setError(null)
    try {
      const token = await stepUp('add_folder')
      await api<Folder>('/api/folders', post({ path: folderPath, step_up_token: token }))
      setPath('')
      load()
    } catch (e) {
      setError(failure(e, 'Adding the folder'))
    }
    setBusy(false)
  }

  async function remove(folder: Folder) {
    setBusy(true)
    setError(null)
    try {
      await api(`/api/folders/${encodeURIComponent(folder.id)}`, { method: 'DELETE' })
      load()
    } catch (e) {
      setError(failure(e, 'Removing the folder'))
    }
    setBusy(false)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (path.trim()) void add(path.trim())
  }

  return (
    <section className="settings-section" aria-labelledby={titleId} aria-busy={busy || undefined}>
      <h2 id={titleId}>Folders Hearthkeep can see</h2>
      <p className="settings-lede">
        Your assistant can find files only in these folders, and never changes them. Adding a folder asks for your
        passkey.
      </p>
      {failed ? (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>Couldn't load your folders.</span>
          <button type="button" className="btn ghost" onClick={load}>
            Retry
          </button>
        </div>
      ) : data === null ? (
        <div className="device-list skeleton" aria-busy="true" aria-label="Loading folders">
          <span className="bar w30" />
          <span className="bar w60" />
        </div>
      ) : (
        <>
          <ul className="device-list" aria-labelledby={titleId}>
            {data.folders.map((folder) => (
              <li key={folder.id} className="device">
                <div className="device-main">
                  <div className="device-text">
                    <p className="device-name">
                      <FilesIcon />
                      <b>{folder.label}</b>
                    </p>
                    <p className="device-meta folder-path">{folder.path}</p>
                  </div>
                  <div className="device-actions">
                    <button
                      type="button"
                      className="btn ghost"
                      aria-label={`Remove ${folder.label}`}
                      disabled={busy}
                      onClick={() => remove(folder)}
                    >
                      Remove
                    </button>
                  </div>
                </div>
              </li>
            ))}
            {data.folders.length === 0 && (
              <li className="device empty">
                <p className="perm-sub">No folders yet. Hearthkeep sees only its own workspace.</p>
              </li>
            )}
          </ul>
          {data.suggestions.length > 0 && (
            <div className="settings-actions" role="group" aria-label="Suggested folders">
              {data.suggestions.map((s) => (
                <button
                  key={s.path}
                  type="button"
                  className="chipbtn"
                  title={s.path}
                  disabled={busy}
                  onClick={() => add(s.path)}
                >
                  Add {s.label}
                </button>
              ))}
            </div>
          )}
          <form className="folder-form" onSubmit={submit}>
            <label className="field">
              <span>Or type a folder path on this PC</span>
              <input
                className="input"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                autoComplete="off"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                maxLength={1024}
                aria-describedby={pathHint}
              />
            </label>
            <button type="submit" className="btn primary" disabled={busy || !path.trim()}>
              <PasskeyIcon />
              Add folder
            </button>
          </form>
          <p className="hint" id={pathHint}>
            The full path, as File Explorer shows it.
          </p>
        </>
      )}
      {error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </div>
      )}
    </section>
  )
}
