import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { Repos } from '../views/Repos'
import { bodyOf, mockFetch, type Handler, type Reply } from '../test/fetch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const now = () => new Date().toISOString()

const row = (over: Record<string, unknown> = {}) => ({
  id: 'r1',
  full_name: 'octo/widgets',
  default_branch: 'main',
  private: true,
  branch: 'hearthkeep/fix',
  changed: ['a.py', 'b.py'],
  ahead: 2,
  behind: 1,
  deps: 'ready',
  last_run: { id: 'run1', status: 'passed', created_at: now() },
  ...over,
})

function routes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'GET /api/github': { body: { git: true, connected: true, login: 'octo' } },
    'GET /api/repos': { body: { repos: [row()] } },
    ...extra,
  })
}

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('step-token')
})

describe('Repos', () => {
  it('shows dependencies state and last test run per repo', async () => {
    routes({
      'GET /api/repos': {
        body: {
          repos: [
            row(),
            row({
              id: 'r2',
              full_name: 'octo/other',
              private: false,
              deps: 'outdated',
              last_run: { id: 'run2', status: 'failed', created_at: now() },
              changed: [],
              ahead: 0,
              behind: 0,
            }),
            row({ id: 'r3', full_name: 'octo/bare', deps: 'none', last_run: null }),
          ],
        },
      },
    })
    render(<Repos />)
    const first = (await screen.findByText('octo/widgets')).closest('li') as HTMLElement
    expect(within(first).getByText('Private')).toBeInTheDocument()
    expect(within(first).getByText('hearthkeep/fix')).toBeInTheDocument()
    expect(within(first).getByText('2 changed files')).toBeInTheDocument()
    expect(within(first).getByText('2 ahead')).toBeInTheDocument()
    expect(within(first).getByText('1 behind')).toBeInTheDocument()
    expect(within(first).getByText('Dependencies ready')).toBeInTheDocument()
    expect(within(first).getByRole('button', { name: /Tests passed/ })).toBeInTheDocument()
    const second = screen.getByText('octo/other').closest('li') as HTMLElement
    expect(within(second).queryByText('Private')).toBeNull()
    expect(within(second).getByText('Dependencies out of date')).toBeInTheDocument()
    expect(within(second).getByRole('button', { name: /Tests failed/ })).toBeInTheDocument()
    const third = screen.getByText('octo/bare').closest('li') as HTMLElement
    expect(within(third).getByText('No tests run yet')).toBeInTheDocument()
    expect(within(third).queryByText(/^Dependencies/)).toBeNull()
  })

  it('opens a run and shows its output as text', async () => {
    routes({
      'GET /api/repo-runs/run1': {
        body: {
          id: 'run1',
          repo_id: 'r1',
          chat_id: 'c',
          command: 'pytest',
          status: 'passed',
          exit_code: 0,
          duration_ms: 1500,
          created_at: now(),
          output: '3 passed <b>ok</b>',
        },
      },
    })
    render(<Repos />)
    fireEvent.click(await screen.findByRole('button', { name: /Tests passed/ }))
    const panel = await screen.findByRole('dialog', { name: 'Test run' })
    expect(await within(panel).findByText('3 passed <b>ok</b>')).toBeInTheDocument()
    expect(within(panel).getByText('pytest')).toBeInTheDocument()
    expect(panel.querySelector('b')).toBeNull()
    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('adding a repo asks for a passkey; bad name shows the server message', async () => {
    let repos = [] as ReturnType<typeof row>[]
    const { calls } = routes({
      'GET /api/repos': () => ({ body: { repos } }),
      'POST /api/repos': (init) => {
        const name = String(JSON.parse(String(init.body)).full_name)
        if (name === 'bad name') {
          return { status: 422, body: { reason: 'name_format', message: 'Use the form owner/name.' } }
        }
        repos = [row({ full_name: name })]
        return { status: 201, body: repos[0] }
      },
    })
    render(<Repos />)
    const input = await screen.findByRole('textbox', { name: 'Repository' })
    expect(screen.getByText(/No repositories yet/)).toBeInTheDocument()
    fireEvent.change(input, { target: { value: 'bad name' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add repository' }))
    expect(await screen.findByText('Use the form owner/name.')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('add_repo')

    fireEvent.change(input, { target: { value: 'octo/widgets' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add repository' }))
    expect(await screen.findByText('octo/widgets')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledTimes(2)
    const posts = calls.filter((c) => c.path === '/api/repos' && c.init.method === 'POST')
    expect(bodyOf(posts[1])).toEqual({ full_name: 'octo/widgets', step_up_token: 'step-token' })
  })

  it('explains GitHub being away in plain words', async () => {
    routes({
      'GET /api/repos': { body: { repos: [] } },
      'POST /api/repos': { status: 502, body: { reason: 'github_unavailable' } },
    })
    render(<Repos />)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Repository' }), {
      target: { value: 'octo/widgets' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add repository' }))
    expect(await screen.findByText(/GitHub isn't answering/)).toBeInTheDocument()
  })

  it('removing a repo confirms, warns about unpushed work, and binds the passkey to repo:<id>', async () => {
    let repos = [row()]
    const { calls } = routes({
      'GET /api/repos': () => ({ body: { repos } }),
      'DELETE /api/repos/r1': () => {
        repos = []
        return { body: { ok: true } }
      },
    })
    render(<Repos />)
    const item = (await screen.findByText('octo/widgets')).closest('li') as HTMLElement
    fireEvent.click(within(item).getByRole('button', { name: 'Remove octo/widgets' }))
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAccessibleDescription(
      "Removing deletes Hearthkeep's copy, including changes that were never pushed.",
    )
    expect(stepUp).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete with passkey' }))
    await waitFor(() => expect(screen.queryByText('octo/widgets')).toBeNull())
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('delete', undefined, 'repo:r1')
    const del = calls.find((c) => c.init.method === 'DELETE')!
    expect(bodyOf(del)).toEqual({ step_up_token: 'step-token' })
  })

  it('updating a repo fetches it and refreshes the row', async () => {
    const { calls } = routes({ 'POST /api/repos/r1/fetch': { body: row({ behind: 0 }) } })
    render(<Repos />)
    const item = (await screen.findByText('octo/widgets')).closest('li') as HTMLElement
    fireEvent.click(within(item).getByRole('button', { name: 'Update octo/widgets' }))
    expect(await within(item).findByText('0 behind')).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/api/repos/r1/fetch' && c.init.method === 'POST')).toBe(true)
    expect(stepUp).not.toHaveBeenCalled()
  })

  it('does not write repo names to the console', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((k) =>
      vi.spyOn(console, k).mockImplementation(() => {}),
    )
    routes({ 'POST /api/repos/r1/fetch': { status: 502, body: { reason: 'fetch_failed', message: 'x' } } })
    render(<Repos />)
    const item = (await screen.findByText('octo/widgets')).closest('li') as HTMLElement
    fireEvent.click(within(item).getByRole('button', { name: 'Update octo/widgets' }))
    await screen.findByRole('alert')
    for (const s of spies) expect(s).not.toHaveBeenCalled()
    spies.forEach((s) => s.mockRestore())
  })

  it('says to connect GitHub first when there is no token', async () => {
    routes({
      'GET /api/github': { body: { git: true, connected: false, login: null } },
      'GET /api/repos': { body: { repos: [] } },
    })
    render(<Repos />)
    expect(await screen.findByText(/Connect GitHub in Settings/)).toBeInTheDocument()
  })
})
