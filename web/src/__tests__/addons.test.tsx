import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { memo } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { useAddons, type Addon } from '../api/addons'
import { AddonsPanel } from '../components/AddonsPanel'
import { AddonsProvider, actionTarget, useAddon, useOpenSettings } from '../shell/AddonsContext'
import { AppShell } from '../shell/AppShell'
import { Settings } from '../views/Settings'
import { forbiddenIn } from '../test/copyRules'
import { addonList, addonsReady, addonsWith, headerOf, healthy, mockFetch, modelsIdle, serverInfo } from '../test/fetch'

const renders = vi.hoisted(() => ({ count: 0 }))

// Memoised, so it re-renders only when the add-ons context it reads changes.
const Probe = memo(function Probe() {
  const github = useAddon('github')
  renders.count += 1
  return <p data-testid="probe">{github ? `github ${github.state}` : 'github unknown'}</p>
})

vi.mock('../views/Placeholder', () => ({
  // A stand-in view that asks the shell to open Settings at an add-on's card.
  Placeholder: () => {
    const open = useOpenSettings()
    return (
      <>
        <button type="button" onClick={() => open('addon-images')}>
          Open the images add-on
        </button>
        <Probe />
      </>
    )
  },
}))

const settingsRoutes = {
  'GET /api/devices': { body: [] },
  'GET /api/folders': { body: { folders: [], suggestions: [] } },
  'GET /api/github': { body: { git: true, connected: false, login: null } },
  'GET /api/settings/startup': { body: { supported: true, enabled: false, command: null } },
  'GET /api/server': serverInfo(),
  ...modelsIdle,
}

function card(id: string): HTMLElement {
  return document.getElementById(`addon-${id}`)!
}

describe('Add-ons', () => {
  it('lists the six add-ons with state, needs and message', async () => {
    mockFetch({ 'GET /api/addons': addonsWith({ analysis: 'docker_stopped', startup: 'unsupported' }) })
    render(<AddonsPanel />)
    expect(await screen.findByRole('heading', { level: 2, name: 'Add-ons' })).toBeInTheDocument()
    const headings = (await screen.findAllByRole('heading', { level: 3 })).map((h) => h.textContent)
    expect(headings).toEqual(['Chat model', 'Analysis', 'Images', 'GitHub', 'Phone and devices', 'Start at login'])

    const analysis = card('analysis')
    expect(analysis).toHaveAttribute('tabindex', '-1')
    expect(within(analysis).getByText('Not set up')).toBeInTheDocument()
    expect(within(analysis).getByText('Python analysis of your files, and reading PDFs and scans.')).toBeInTheDocument()
    expect(
      within(analysis).getByText('Needs: Docker Desktop, and the analysis sandbox (built once, about 1 GB).'),
    ).toBeInTheDocument()
    expect(within(analysis).getByText("Docker Desktop isn't running. Start it, then check again.")).toBeInTheDocument()

    expect(within(card('chat')).getByText('Ready', { selector: '.badge' })).toHaveClass('green')
    // A ready add-on says no more than its badge.
    expect(within(card('chat')).queryByText('Ready.')).toBeNull()
    expect(within(card('startup')).getByText('Only on Windows')).toBeInTheDocument()
  })

  it('gives every state its badge', async () => {
    const states: [string, string, string][] = [
      ['chat', 'starting', 'Loading'],
      ['chat', 'offline', 'Offline'],
      ['chat', 'error', 'Needs attention'],
      ['devices', 'this_pc_only', 'This PC only'],
      ['startup', 'off', 'Off'],
      ['startup', 'on', 'On'],
    ]
    for (const [id, state, label] of states) {
      mockFetch({ 'GET /api/addons': addonsWith({ [id]: state }) })
      const { unmount } = render(<AddonsPanel />)
      await screen.findByRole('heading', { level: 3, name: 'Chat model' })
      expect(within(card(id)).getByText(label, { selector: '.badge' })).toBeInTheDocument()
      unmount()
    }
  })

  it('check again posts refresh and shows the new state', async () => {
    let refreshed = false
    const { calls } = mockFetch({
      'GET /api/addons': () => addonsWith({ analysis: refreshed ? 'ready' : 'docker_stopped' }),
      'POST /api/addons/refresh': () => {
        refreshed = true
        return addonsWith({ analysis: 'ready' })
      },
    })
    render(<AddonsPanel />)
    await screen.findByText("Docker Desktop isn't running. Start it, then check again.")
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(within(card('analysis')).getByText('Ready', { selector: '.badge' })).toBeInTheDocument())
    const post = calls.find((c) => c.path === '/api/addons/refresh')!
    expect(post.init.method).toBe('POST')
    expect(headerOf(post, 'X-Hearthkeep')).toBe('1')
    expect(screen.queryByText("Docker Desktop isn't running. Start it, then check again.")).toBeNull()
  })

  it('check again that fails says so and keeps what was shown', async () => {
    mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'docker_stopped' }),
      'POST /api/addons/refresh': { status: 500, body: { reason: 'boom' } },
    })
    render(<AddonsPanel />)
    await screen.findByText("Docker Desktop isn't running. Start it, then check again.")
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't check the add-ons. Try again.")
    expect(within(card('analysis')).getByText('Not set up')).toBeInTheDocument()
  })

  it('add token goes to the GitHub panel', async () => {
    mockFetch({ ...settingsRoutes, 'GET /api/addons': addonsWith({ github: 'needs_token' }) })
    render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add a GitHub token' }))
    await waitFor(() => expect(document.getElementById('settings-github')).toHaveFocus())
  })

  it('start at login leads to its panel', async () => {
    mockFetch({ ...settingsRoutes, 'GET /api/addons': addonsWith({ startup: 'off' }) })
    render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Start at login settings' }))
    await waitFor(() => expect(document.getElementById('settings-startup')).toHaveFocus())
  })

  it('analysis links to shared folders', async () => {
    mockFetch({ ...settingsRoutes, 'GET /api/addons': addonsReady })
    render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Shared folders' }))
    await waitFor(() => expect(document.getElementById('settings-folders')).toHaveFocus())
    // Only the analysis card has the link.
    expect(screen.getAllByRole('button', { name: 'Shared folders' })).toHaveLength(1)
  })

  it.each(['not_set_up', 'docker_stopped', 'needs_image'])(
    'analysis %s shows no Shared folders button: it would not fix the problem',
    async (state) => {
      mockFetch({ 'GET /api/addons': addonsWith({ analysis: state }) })
      render(<AddonsPanel />)
      const analysis = await waitFor(() => {
        expect(card('analysis')).not.toBeNull()
        return card('analysis')
      })
      expect(within(analysis).getByText(addonList({ analysis: state })[1].message)).toBeInTheDocument()
      expect(within(analysis).queryByRole('button')).toBeNull()
      expect(screen.queryByRole('button', { name: 'Shared folders' })).toBeNull()
    },
  )

  it('openSettings from the shell focuses the add-on card', async () => {
    mockFetch({
      ...settingsRoutes,
      'GET /api/health': healthy,
      'GET /api/addons': addonsWith({ images: 'not_set_up' }),
    })
    render(<AppShell initialView="today" onSignOut={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open the images add-on' }))
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    await waitFor(() => expect(card('images')).toHaveFocus())
  })

  it('a later visit to Settings does not jump again', async () => {
    mockFetch({
      ...settingsRoutes,
      'GET /api/health': healthy,
      'GET /api/addons': addonsReady,
    })
    render(<AppShell initialView="today" onSignOut={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open the images add-on' }))
    await waitFor(() => expect(card('images')).toHaveFocus())
    const sidebar = screen.getByRole('navigation', { name: 'Hearthkeep' })
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Images' }))
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Settings' }))
    await screen.findByRole('heading', { level: 3, name: 'Images' })
    expect(card('images')).not.toHaveFocus()
  })

  it('unknown add-ons never gate: a 404 leaves views as today', async () => {
    mockFetch({ 'GET /api/addons': { status: 404, body: { reason: 'not_found' } } })
    function Probe() {
      const state = useAddons()
      return (
        <AddonsProvider value={{ ...state, openSettings: vi.fn() }}>
          <p>{state.failed ? 'load failed' : 'loading'}</p>
          <Show />
        </AddonsProvider>
      )
    }
    function Show() {
      const github: Addon | null = useAddon('github')
      return <p>{github ? `github is ${github.state}` : 'github is unknown'}</p>
    }
    render(<Probe />)
    await screen.findByText('load failed')
    expect(screen.getByText('github is unknown')).toBeInTheDocument()
    // Without any provider, too.
    function Bare() {
      return <p>{useAddon('chat') === null ? 'chat is unknown' : 'chat is known'}</p>
    }
    render(<Bare />)
    expect(screen.getByText('chat is unknown')).toBeInTheDocument()
  })

  it('says when a check again worked', async () => {
    mockFetch({
      'GET /api/addons': addonsReady,
      'POST /api/addons/refresh': addonsReady,
    })
    render(<AddonsPanel />)
    await screen.findByRole('heading', { level: 3, name: 'Chat model' })
    expect(screen.queryByText('Checked just now')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Checked just now')).toBeInTheDocument()
  })

  it('consumers do not re-render when unrelated shell state changes', async () => {
    mockFetch({ ...settingsRoutes, 'GET /api/health': healthy, 'GET /api/addons': addonsReady })
    renders.count = 0
    render(<AppShell initialView="today" onSignOut={() => {}} />)
    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('github ready'))
    const before = renders.count
    // The tray opening is shell state unrelated to add-ons.
    fireEvent.click(screen.getByRole('button', { name: 'Sent to you' }))
    await Promise.resolve()
    expect(renders.count).toBe(before)
  })

  it('an anchor given while loading failed does not linger', async () => {
    let ok = false
    mockFetch({
      ...settingsRoutes,
      'GET /api/health': healthy,
      'GET /api/addons': () => (ok ? addonsReady : { status: 500, body: { reason: 'boom' } }),
      'POST /api/addons/refresh': addonsReady,
    })
    render(<AppShell initialView="today" onSignOut={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open the images add-on' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    ok = true
    // Away and back, with the add-ons now loaded: nothing jumps to the card.
    const sidebar = screen.getByRole('navigation', { name: 'Hearthkeep' })
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Images' }))
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Settings' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    await screen.findByRole('heading', { level: 3, name: 'Images' })
    await new Promise((r) => setTimeout(r, 30)) // let a stray focus move happen
    expect(card('images')).not.toHaveFocus()
  })

  it('maps actions to the screen that can act', () => {
    const [, analysis, , github, , startup] = addonList({
      analysis: 'docker_stopped',
      github: 'needs_token',
      startup: 'off',
    }) as Addon[]
    expect(actionTarget(analysis)).toBe('addon-analysis')
    expect(actionTarget(github)).toBe('settings-github')
    expect(actionTarget(startup)).toBe('settings-startup')
  })

  it('never shows a command or placeholder', async () => {
    const states = [
      { chat: 'not_set_up', analysis: 'not_set_up', images: 'error', github: 'not_set_up', devices: 'needs_tailscale', startup: 'off' },
      { chat: 'error', analysis: 'needs_image', images: 'not_set_up', github: 'needs_git', devices: 'this_pc_only', startup: 'unsupported' },
      { chat: 'offline', analysis: 'docker_stopped', github: 'needs_token' },
    ]
    for (const s of states) {
      mockFetch({ 'GET /api/addons': addonsWith(s) })
      const { container, unmount } = render(<AddonsPanel />)
      await screen.findByRole('heading', { level: 3, name: 'Chat model' })
      expect(forbiddenIn(container.textContent ?? '')).toEqual([])
      unmount()
    }
  })
})
