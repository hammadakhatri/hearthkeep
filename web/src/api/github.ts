/** GitHub: the token, the repos Hearthkeep has a copy of, and their test runs. */

import { api, post } from './client'
import { stepUp } from './webauthn'

export type GitHubState = { git: boolean; connected: boolean; login: string | null }

export type RunStatus = 'passed' | 'failed' | 'timed_out' | 'error'
export type DepsState = 'none' | 'needed' | 'ready' | 'outdated'

export type RepoRow = {
  id: string
  full_name: string
  default_branch: string
  private: boolean
  /** Null when the clone is missing or broken. */
  branch: string | null
  changed: string[]
  ahead: number
  behind: number
  deps: DepsState | null
  last_run: { id: string; status: RunStatus; created_at: string } | null
}

export type RunSummary = {
  id: string
  chat_id: string | null
  command: string
  status: RunStatus
  exit_code: number | null
  duration_ms: number | null
  created_at: string
}

export type RunDetail = RunSummary & { repo_id: string; output: string }

const enc = encodeURIComponent

export const getGitHub = () => api<GitHubState>('/api/github')

/** Save or replace the token; asks for a passkey first (it widens what Hearthkeep can reach). */
export async function saveToken(token: string): Promise<{ connected: boolean; login: string }> {
  const step = await stepUp('github_token')
  return api('/api/github/token', {
    method: 'PUT',
    body: JSON.stringify({ token, step_up_token: step }),
  })
}

/** Removing narrows access: the session is enough. */
export const removeToken = () => api<{ connected: boolean; login: null }>('/api/github/token', { method: 'DELETE' })

export const listRepos = () => api<{ repos: RepoRow[] }>('/api/repos').then((r) => r.repos)

export async function addRepo(fullName: string): Promise<RepoRow> {
  const step = await stepUp('add_repo')
  return api<RepoRow>('/api/repos', post({ full_name: fullName, step_up_token: step }))
}

/** Removing deletes the clone, so the passkey is bound to exactly this repo. */
export async function removeRepo(id: string): Promise<void> {
  const step = await stepUp('delete', undefined, `repo:${id}`)
  await api(`/api/repos/${enc(id)}`, {
    method: 'DELETE',
    body: JSON.stringify({ step_up_token: step }),
  })
}

export const fetchRepo = (id: string) => api<RepoRow>(`/api/repos/${enc(id)}/fetch`, { method: 'POST' })

export const listRuns = (id: string) => api<{ runs: RunSummary[] }>(`/api/repos/${enc(id)}/runs`).then((r) => r.runs)

export const getRun = (runId: string) => api<RunDetail>(`/api/repo-runs/${enc(runId)}`)
