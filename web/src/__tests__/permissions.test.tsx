import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { ToolsPermissions } from '../views/ToolsPermissions'
import { bodyOf, mockFetch } from '../test/fetch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const demo = {
  tool: 'demo',
  level: 'ask',
  effective_level: 'ask',
  description: 'Get the current local date and time. Save a short note for the owner to read later.',
  tools: ['clock', 'note'],
}
const python = { tool: 'python', level: 'allowed', effective_level: 'ask', description: 'Run Python.', tools: ['python'] }

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('settings-token')
})

describe('Tools & permissions', () => {
  it('lists each tool with its description and current level', async () => {
    mockFetch({ 'GET /api/settings/tools': { body: [demo, python] } })
    render(<ToolsPermissions />)
    const row = (await screen.findByText('Demo tools')).closest('li')!
    expect(within(row).getByText(demo.description)).toBeInTheDocument()
    const levels = within(row).getByRole('radiogroup', { name: 'Demo tools' })
    expect(within(levels).getAllByRole('radio').map((r) => r.textContent)).toEqual(['Allowed', 'Ask', 'Off'])
    expect(within(levels).getByRole('radio', { name: 'Ask' })).toHaveAttribute('aria-checked', 'true')
    expect(within(row).queryByText(/Needs Docker/)).toBeNull()
  })

  it('shows a Docker note when the effective level differs', async () => {
    mockFetch({ 'GET /api/settings/tools': { body: [demo, python] } })
    render(<ToolsPermissions />)
    const row = (await screen.findByText('Python')).closest('li')!
    expect(within(row).getByText('Needs Docker to run freely')).toBeInTheDocument()
  })

  it('widening a tool level asks for step-up; narrowing does not', async () => {
    let current = { ...demo }
    const { calls } = mockFetch({
      'GET /api/settings/tools': { body: [demo] },
      'PUT /api/settings/tools/demo': (init) => {
        const level = bodyOf({ path: '', init }).level as string
        current = { ...current, level, effective_level: level }
        return { body: current }
      },
    })
    render(<ToolsPermissions />)
    const levels = await screen.findByRole('radiogroup', { name: 'Demo tools' })

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

  it('keeps the level when Face ID / Touch ID is cancelled', async () => {
    vi.mocked(stepUp).mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    const { calls } = mockFetch({ 'GET /api/settings/tools': { body: [demo] } })
    render(<ToolsPermissions />)
    const levels = await screen.findByRole('radiogroup', { name: 'Demo tools' })
    fireEvent.click(within(levels).getByRole('radio', { name: 'Allowed' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Change was cancelled.')
    expect(within(levels).getByRole('radio', { name: 'Ask' })).toHaveAttribute('aria-checked', 'true')
    expect(calls.filter((c) => c.init.method === 'PUT')).toHaveLength(0)
  })
})
