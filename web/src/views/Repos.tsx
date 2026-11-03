import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { isApiError } from '../api/client'
import {
  addRepo,
  fetchRepo,
  getGitHub,
  getRun,
  listRepos,
  listRuns,
  removeRepo,
  type DepsState,
  type GitHubState,
  type RepoRow,
  type RunDetail,
  type RunStatus,
  type RunSummary,
} from '../api/github'
import { passkeyErrorMessage } from '../api/webauthn'
import { ConfirmDelete } from '../components/ConfirmDelete'
import { relativeTime } from '../components/DevicesPanel'
import { AlertIcon, MoreIcon, PasskeyIcon, ReposIcon } from '../shell/icons'

const REMOVE_WARNING = "Removing deletes Hearthkeep's copy, including changes that were never pushed."

const RUN_LABEL: Record<RunStatus, string> = {
  passed: 'Tests passed',
  failed: 'Tests failed',
  timed_out: 'Tests timed out',
  error: 'Tests could not run',
}

const DEPS_LABEL: Record<DepsState, string | null> = {
  none: null,
  needed: 'Dependencies not installed',
  ready: 'Dependencies ready',
  outdated: 'Dependencies out of date',
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

function serverMessage(error: unknown): string | null {
  const message = isApiError(error) ? (error.body as { message?: unknown } | null)?.message : null
  return typeof message === 'string' && message ? message : null
}

/** A plain sentence for anything adding, updating or removing a repo can throw. */
function failure(error: unknown, action: string): string {
  if (isApiError(error, 'github_unavailable')) {
    return "GitHub isn't answering right now. It may be down, rate limiting you, or the token was rejected."
  }
  if (isApiError(error, 'no_token')) return 'Connect GitHub in Settings first.'
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  if (isApiError(error, 'step_up_required')) return "Your passkey couldn't be confirmed. Try again."
  if (isApiError(error, 'unavailable')) return 'GitHub is not available on this computer.'
  if (isApiError(error)) return serverMessage(error) ?? `${action} didn't work. Try again.`
  return passkeyErrorMessage(error, action)
}

function RunPanel({ repo, runId, onClose }: { repo: RepoRow; runId: string; onClose: () => void }) {
  const titleId = useId()
  const close = useRef<HTMLButtonElement>(null)
  const [currentId, setCurrentId] = useState(runId)
  const [run, setRun] = useState<RunDetail | null>(null)
  const [failed, setFailed] = useState(false)
  const [runs, setRuns] = useState<RunSummary[]>([])

  useEffect(() => {
    close.current?.focus()
    let live = true
    listRuns(repo.id)
      .then((r) => live && setRuns(r))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [repo.id])

  useEffect(() => {
    let live = true
    setRun(null)
    setFailed(false)
    getRun(currentId)
      .then((r) => live && setRun(r))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [currentId])

  return (
    <div className="scrim confirm-scrim" onClick={onClose}>
      <div
        className="run-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            onClose()
          }
        }}
      >
        <div className="run-head">
          <h2 id={titleId}>Test run</h2>
          <button ref={close} type="button" className="btn ghost" onClick={onClose}>
            Close
          </button>
        </div>
        {failed ? (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>Couldn't load that run.</span>
          </div>
        ) : run === null ? (
          <div className="device-list skeleton" aria-busy="true" aria-label="Loading the run">
            <span className="bar w30" />
            <span className="bar w60" />
          </div>
        ) : (
          <>
            <p className="run-meta">
              <span className={`badge ${run.status === 'passed' ? 'green' : 'red'}`}>{RUN_LABEL[run.status]}</span>
              <span>{relativeTime(run.created_at)}</span>
              {run.duration_ms !== null && <span>{(run.duration_ms / 1000).toFixed(1)} s</span>}
            </p>
            <p className="run-command">
              <code>{run.command}</code>
            </p>
            <pre className="run-output" tabIndex={0} aria-label="Test output">
              <code>{run.output || '(no output)'}</code>
            </pre>
          </>
        )}
        {runs.length > 1 && (
          <div className="run-others" role="group" aria-label="Earlier runs">
            {runs.map((r) => (
              <button
                key={r.id}
                type="button"
                className="chipbtn"
                aria-pressed={r.id === currentId}
                onClick={() => setCurrentId(r.id)}
              >
                {RUN_LABEL[r.status]} {relativeTime(r.created_at)}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function RepoItem({
  repo,
  busy,
  error,
  onUpdate,
  onRemove,
  onOpenRun,
}: {
  repo: RepoRow
  busy: boolean
  error: string | null
  onUpdate: () => void
  onRemove: () => void
  onOpenRun: (runId: string) => void
}) {
  const [menu, setMenu] = useState(false)
  const deps = repo.deps ? DEPS_LABEL[repo.deps] : null
  const run = repo.last_run
  return (
    <li className="repo" aria-busy={busy || undefined}>
      <div className="repo-main">
        <div className="repo-text">
          <p className="repo-name">
            <ReposIcon />
            <b>{repo.full_name}</b>
            {repo.private && <span className="badge">Private</span>}
          </p>
          <p className="repo-meta">
            {repo.branch ? <code className="repo-branch">{repo.branch}</code> : <span>Copy unavailable</span>}
            <span>{plural(repo.changed.length, 'changed file', 'changed files')}</span>
            <span>{repo.ahead} ahead</span>
            <span>{repo.behind} behind</span>
          </p>
          <p className="repo-meta">
            {deps && <span>{deps}</span>}
            {run ? (
              <button
                type="button"
                className={`repo-run ${run.status}`}
                aria-label={`${RUN_LABEL[run.status]} ${relativeTime(run.created_at)}. Show output`}
                onClick={() => onOpenRun(run.id)}
              >
                {RUN_LABEL[run.status]} {relativeTime(run.created_at)}
              </button>
            ) : (
              <span>No tests run yet</span>
            )}
          </p>
        </div>
        <button
          type="button"
          className="btn ghost repo-menu-btn"
          aria-label={`Actions for ${repo.full_name}`}
          aria-expanded={menu}
          onClick={() => setMenu(!menu)}
        >
          <MoreIcon />
        </button>
        <div className={`repo-actions${menu ? ' open' : ''}`}>
          <button
            type="button"
            className="btn"
            aria-label={`Update ${repo.full_name}`}
            disabled={busy}
            onClick={() => {
              setMenu(false)
              onUpdate()
            }}
          >
            Update
          </button>
          <button
            type="button"
            className="btn ghost danger-text"
            aria-label={`Remove ${repo.full_name}`}
            disabled={busy}
            onClick={() => {
              setMenu(false)
              onRemove()
            }}
          >
            Remove
          </button>
        </div>
      </div>
      {error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </div>
      )}
    </li>
  )
}

/** The repositories Hearthkeep keeps a copy of: add, update, remove, and read the last test run. */
export function Repos() {
  const titleId = useId()
  const [github, setGitHub] = useState<GitHubState | null>(null)
  const [repos, setRepos] = useState<RepoRow[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [name, setName] = useState('')
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [removing, setRemoving] = useState<RepoRow | null>(null)
  const [run, setRun] = useState<{ repo: RepoRow; runId: string } | null>(null)

  const load = useCallback(() => {
    setFailed(false)
    Promise.all([getGitHub(), listRepos()])
      .then(([state, list]) => {
        setGitHub(state)
        setRepos(list)
      })
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  async function add(event: FormEvent) {
    event.preventDefault()
    const value = name.trim()
    if (!value || adding) return
    setAdding(true)
    setAddError(null)
    try {
      const repo = await addRepo(value)
      setRepos((list) => [...(list ?? []).filter((r) => r.id !== repo.id), repo])
      setName('')
    } catch (e) {
      setAddError(failure(e, 'Adding the repository'))
    }
    setAdding(false)
  }

  async function update(repo: RepoRow) {
    setBusyId(repo.id)
    setRowErrors(({ [repo.id]: _gone, ...rest }) => rest)
    try {
      const fresh = await fetchRepo(repo.id)
      setRepos((list) => (list ?? []).map((r) => (r.id === repo.id ? fresh : r)))
    } catch (e) {
      setRowErrors((errors) => ({ ...errors, [repo.id]: failure(e, 'Updating the repository') }))
    }
    setBusyId(null)
  }

  const canAdd = github !== null && github.git && github.connected

  return (
    <div className="repos">
      <section className="settings-section" aria-labelledby={titleId}>
        <h2 id={titleId}>Your repositories</h2>
        {failed ? (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>Couldn't load your repositories.</span>
            <button type="button" className="btn ghost" onClick={load}>
              Retry
            </button>
          </div>
        ) : repos === null || github === null ? (
          <div className="device-list skeleton" aria-busy="true" aria-label="Loading repositories">
            <span className="bar w30" />
            <span className="bar w60" />
          </div>
        ) : (
          <>
            {!github.git && (
              <div className="note info">
                <span>Install Git to use GitHub with Hearthkeep.</span>
              </div>
            )}
            {github.git && !github.connected && (
              <div className="note info">
                <span>Connect GitHub in Settings to add repositories.</span>
              </div>
            )}
            {repos.length > 0 && (
              <ul className="device-list repo-list" aria-labelledby={titleId}>
                {repos.map((repo) => (
                  <RepoItem
                    key={repo.id}
                    repo={repo}
                    busy={busyId === repo.id}
                    error={rowErrors[repo.id] ?? null}
                    onUpdate={() => void update(repo)}
                    onRemove={() => setRemoving(repo)}
                    onOpenRun={(runId) => setRun({ repo, runId })}
                  />
                ))}
              </ul>
            )}
            {repos.length === 0 && canAdd && (
              <p className="settings-lede">
                No repositories yet. Add one by its GitHub name and Hearthkeep keeps a copy on this computer. Adding
                asks for your passkey.
              </p>
            )}
            {canAdd && (
              <form className="folder-form" onSubmit={(e) => void add(e)}>
                <label className="field">
                  <span>Repository</span>
                  <input
                    className="input"
                    value={name}
                    placeholder="owner/name"
                    onChange={(e) => setName(e.target.value)}
                    autoComplete="off"
                    autoCorrect="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    maxLength={200}
                  />
                </label>
                <button type="submit" className="btn primary" disabled={adding || !name.trim()}>
                  <PasskeyIcon />
                  Add repository
                </button>
              </form>
            )}
            {addError && (
              <div className="note error" role="alert">
                <AlertIcon />
                <span>{addError}</span>
              </div>
            )}
          </>
        )}
      </section>
      {removing && (
        <ConfirmDelete
          title={`Remove ${removing.full_name}?`}
          body={REMOVE_WARNING}
          onCancel={() => setRemoving(null)}
          onConfirm={async () => {
            await removeRepo(removing.id)
            setRepos((list) => (list ?? []).filter((r) => r.id !== removing.id))
            setRemoving(null)
          }}
        />
      )}
      {run && <RunPanel repo={run.repo} runId={run.runId} onClose={() => setRun(null)} />}
    </div>
  )
}
