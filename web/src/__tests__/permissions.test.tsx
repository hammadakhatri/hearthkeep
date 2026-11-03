import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { ToolsPermissions } from '../views/ToolsPermissions'
import { bodyOf, mockFetch } from '../test/fetch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const github = {
  tool: 'github',
  level: 'ask',
  effective_level: 'ask',
  description: 'Read your GitHub repositories and open pull requests.',
  tools: ['github_read'],
  addon: null,
}
const python = {
  tool: 'python',
  level: 'allowed',
  effective_level: 'allowed',
  description: 'Run Python.',
  tools: [],
  addon: {
    id: 'analysis',
    state: 'docker_stopped',
    ready: false,
    short: 'Needs Docker',
    message: "Docker Desktop isn't running. Start it, then check again.",
  },
}

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('settings-token')
})

describe('Tools & permissions', () => {
  it('lists each tool with its description and current level', async () => {
    mockFetch({ 'GET /api/settings/tools': { body: [github, python] } })
    render(<ToolsPermissions />)
    const row = (await screen.findByText('GitHub')).closest('li')!
    expect(within(row).getByText(github.description)).toBeInTheDocument()
    const levels = within(row).getByRole('radiogroup', { name: 'GitHub' })
    expect(within(levels).getAllByRole('radio').map((r) => r.textContent)).toEqual(['Allowed', 'Ask', 'Off'])
    expect(within(levels).getByRole('radio', { name: 'Ask' })).toHaveAttribute('aria-checked', 'true')
    expect(within(row).queryByText(/Needs Docker/)).toBeNull()
  })


  it('widening a tool level asks for step-up; narrowing does not', async () => {
    let current = { ...github }
    const { calls } = mockFetch({
      'GET /api/settings/tools': { body: [github] },
      'PUT /api/settings/tools/github': (init) => {
        const level = bodyOf({ path: '', init }).level as string
        current = { ...current, level, effective_level: level }
        return { body: current }
      },
    })
    render(<ToolsPermissions />)
    const levels = await screen.findByRole('radiogroup', { name: 'GitHub' })

    fireEvent.click(within(levels).getByRole('radio', { name: 'Allowed' }))
    await screen.findByRole('radio', { name: 'Allowed', checked: true })
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('settings')
    const widen = calls.filter((c) => c.init.method === 'PUT')
    expect(bodyOf(widen[0])).toEqual({ level: 'allowed', step_up_token: 'settings-token' })

    fireEvent.click(within(levels).getByRole('radio', { name: 'Off' }))
    await screen.findByRole('radio', { name: 'Off', checked: true })
    expect(stepUp).toHaveBeenCalledTimes(1)
    const narrow = calls.filter((c) => c.init.method === 'PUT')
    expect(bodyOf(narrow[1])).toEqual({ level: 'off' })
  })

  it('keeps the level when the passkey is cancelled', async () => {
    vi.mocked(stepUp).mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    const { calls } = mockFetch({ 'GET /api/settings/tools': { body: [github] } })
    render(<ToolsPermissions />)
    const levels = await screen.findByRole('radiogroup', { name: 'GitHub' })
    fireEvent.click(within(levels).getByRole('radio', { name: 'Allowed' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Change was cancelled.')
    expect(within(levels).getByRole('radio', { name: 'Ask' })).toHaveAttribute('aria-checked', 'true')
    expect(calls.filter((c) => c.init.method === 'PUT')).toHaveLength(0)
  })
})
