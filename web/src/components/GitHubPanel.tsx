import { useCallback, useEffect, useId, useState, type FormEvent } from 'react'
import { isApiError } from '../api/client'
import { getGitHub, removeToken, saveToken, type GitHubState } from '../api/github'
import { PASSKEY_NOT_CONFIRMED, passkeyErrorMessage } from '../api/webauthn'
import { useOpenSettings } from '../shell/AddonsContext'
import { AlertIcon, PasskeyIcon } from '../shell/icons'

/** GitHub's own page for making a fine-grained token; the install guide's "Add GitHub" section says which permissions to pick. */
export const TOKEN_URL = 'https://github.com/settings/personal-access-tokens/new'

function serverMessage(error: unknown): string | null {
  const message = isApiError(error) ? (error.body as { message?: unknown } | null)?.message : null
  return typeof message === 'string' && message ? message : null
}

/** A plain sentence for anything saving or removing the token can throw. */
export function tokenErrorMessage(error: unknown, action: string): string {
  if (isApiError(error, 'token_format')) {
    return 'That is not a fine-grained token. It starts with github_pat_.'
  }
  if (isApiError(error, 'token_rejected')) return serverMessage(error) ?? 'GitHub did not accept that token.'
  if (isApiError(error, 'github_unavailable')) {
    return "GitHub isn't answering right now. Wait a moment and try again."
  }
  if (isApiError(error, 'secrets_unreadable')) return serverMessage(error) ?? "Hearthkeep couldn't use its stored secrets."
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  if (isApiError(error, 'step_up_required')) return PASSKEY_NOT_CONFIRMED
  if (isApiError(error)) return `${action} didn't work. Try again.`
  return passkeyErrorMessage(error, action)
}

/** The GitHub token: save or replace it (passkey), or remove it (session only). */
export function GitHubPanel() {
  const titleId = useId()
  const openSettings = useOpenSettings()
  const [state, setState] = useState<GitHubState | null>(null)
  const [failed, setFailed] = useState(false)
  const [token, setToken] = useState('')
  const [replacing, setReplacing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setFailed(false)
    getGitHub()
      .then(setState)
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  async function save(event: FormEvent) {
    event.preventDefault()
    const value = token.trim()
    if (!value || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await saveToken(value)
      setToken('')
      setReplacing(false)
      setState((s) => ({ git: s?.git ?? true, connected: result.connected, login: result.login }))
    } catch (e) {
      setError(tokenErrorMessage(e, 'Saving the token'))
    }
    setBusy(false)
  }

  async function remove() {
    setBusy(true)
    setError(null)
    try {
      await removeToken()
      setToken('')
      setReplacing(false)
      setState((s) => ({ git: s?.git ?? true, connected: false, login: null }))
    } catch (e) {
      setError(tokenErrorMessage(e, 'Removing the token'))
    }
    setBusy(false)
  }

  const showForm = state !== null && state.git && (!state.connected || replacing)

  return (
    <section
      id="settings-github"
      className="settings-section"
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
      tabIndex={-1}
    >
      <h2 id={titleId}>GitHub</h2>
      <p className="settings-lede">
        With a token, your assistant can read your repositories, change them on your computer, and push only after
        you approve. Saving a token asks for your passkey.
      </p>
      {failed ? (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>Couldn't load GitHub.</span>
          <button type="button" className="btn ghost" onClick={load}>
            Retry
          </button>
        </div>
      ) : state === null ? (
        <div className="device-list skeleton" aria-busy="true" aria-label="Loading GitHub">
          <span className="bar w30" />
          <span className="bar w60" />
        </div>
      ) : !state.git ? (
        <div className="note info">
          <span>Install Git to use GitHub with Hearthkeep.</span>
          <button type="button" className="btn" onClick={() => openSettings('addon-github')}>
            Set up
          </button>
        </div>
      ) : (
        <>
          {state.connected && (
            <div className="tile github-connected">
              <p className="perm-sub">
                <span className="badge green">Connected as @{state.login}</span>
              </p>
              <div className="settings-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  aria-expanded={replacing}
                  onClick={() => setReplacing(!replacing)}
                >
                  Replace token
                </button>
                <button type="button" className="btn ghost" disabled={busy} onClick={() => void remove()}>
                  Remove token
                </button>
              </div>
            </div>
          )}
          {showForm && (
            <form className="folder-form" onSubmit={(e) => void save(e)}>
              <label className="field">
                <span>Fine-grained access token</span>
                <input
                  className="input"
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  autoComplete="off"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  maxLength={512}
                />
              </label>
              <button type="submit" className="btn primary" disabled={busy || !token.trim()}>
                <PasskeyIcon />
                Save token
              </button>
            </form>
          )}
          {showForm && (
            <p className="hint">
              Not sure how to make one?{' '}
              <a href={TOKEN_URL} target="_blank" rel="noreferrer noopener">
                Create a token
              </a>{' '}
              on GitHub. The install guide's "Add GitHub" section lists the permissions to choose.
            </p>
          )}
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
