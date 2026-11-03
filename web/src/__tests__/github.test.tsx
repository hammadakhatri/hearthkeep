import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { GitHubPanel } from '../components/GitHubPanel'
import { bodyOf, mockFetch, type Handler, type Reply } from '../test/fetch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const TOKEN = 'github_pat_' + 'A'.repeat(30)

function routes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'GET /api/github': { body: { git: true, connected: false, login: null } },
    ...extra,
  })
}

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('step-token')
})

describe('GitHubPanel', () => {
  it('saving a token asks for a passkey and shows the login', async () => {
    const { calls } = routes({ 'PUT /api/github/token': { body: { connected: true, login: 'octo' } } })
    render(<GitHubPanel />)
    const field = await screen.findByLabelText('Fine-grained access token')
    expect(field).toHaveAttribute('type', 'password')
    expect(field).toHaveAttribute('autocomplete', 'off')
    expect(screen.getByRole('link', { name: /create a token/i })).toHaveAttribute(
      'href',
      expect.stringContaining('github.com/settings/personal-access-tokens'),
    )
    fireEvent.change(field, { target: { value: TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    expect(await screen.findByText('Connected as @octo')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('github_token')
    const put = calls.find((c) => c.init.method === 'PUT')!
    expect(bodyOf(put)).toEqual({ token: TOKEN, step_up_token: 'step-token' })
    // The token is not kept on screen or in the page afterwards.
    expect(screen.queryByLabelText('Fine-grained access token')).toBeNull()
    expect(document.body.textContent).not.toContain(TOKEN)
  })

  it('shows the server message when the token is rejected', async () => {
    routes({
      'PUT /api/github/token': {
        status: 422,
        body: { reason: 'token_rejected', message: 'GitHub did not accept that token.' },
      },
    })
    render(<GitHubPanel />)
    fireEvent.change(await screen.findByLabelText('Fine-grained access token'), { target: { value: TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    expect(await screen.findByText('GitHub did not accept that token.')).toBeInTheDocument()
  })

  it('says what is wrong with the token format', async () => {
    routes({ 'PUT /api/github/token': { status: 422, body: { reason: 'token_format' } } })
    render(<GitHubPanel />)
    fireEvent.change(await screen.findByLabelText('Fine-grained access token'), { target: { value: 'nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/fine-grained token/i)
  })

  it('says GitHub is away on a 502', async () => {
    routes({ 'PUT /api/github/token': { status: 502, body: { reason: 'github_unavailable' } } })
    render(<GitHubPanel />)
    fireEvent.change(await screen.findByLabelText('Fine-grained access token'), { target: { value: TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/GitHub isn't answering/)
  })

  it('removing the token needs no passkey', async () => {
    const { calls } = routes({
      'GET /api/github': { body: { git: true, connected: true, login: 'octo' } },
      'DELETE /api/github/token': { body: { connected: false, login: null } },
    })
    render(<GitHubPanel />)
    expect(await screen.findByText('Connected as @octo')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove token' }))
    expect(await screen.findByLabelText('Fine-grained access token')).toBeInTheDocument()
    expect(stepUp).not.toHaveBeenCalled()
    expect(calls.some((c) => c.init.method === 'DELETE' && c.path === '/api/github/token')).toBe(true)
  })

  it('replacing the token asks for a passkey', async () => {
    const { calls } = routes({
      'GET /api/github': { body: { git: true, connected: true, login: 'octo' } },
      'PUT /api/github/token': { body: { connected: true, login: 'octo2' } },
    })
    render(<GitHubPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Replace token' }))
    fireEvent.change(screen.getByLabelText('Fine-grained access token'), { target: { value: TOKEN } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    expect(await screen.findByText('Connected as @octo2')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('github_token')
    expect(calls.filter((c) => c.init.method === 'PUT')).toHaveLength(1)
  })

  it('says to install Git when git is missing', async () => {
    routes({ 'GET /api/github': { body: { git: false, connected: false, login: null } } })
    render(<GitHubPanel />)
    expect(await screen.findByText('Install Git to use GitHub with Hearthkeep.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Fine-grained access token')).toBeNull()
  })

  it('offers a retry when the state cannot be loaded', async () => {
    routes({ 'GET /api/github': { status: 500, body: { reason: 'boom' } } })
    render(<GitHubPanel />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument())
  })
})
