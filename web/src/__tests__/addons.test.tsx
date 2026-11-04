import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { memo } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useAddons, type Addon } from '../api/addons'
import { stepUp } from '../api/webauthn'
import { AddonsPanel } from '../components/AddonsPanel'
import { SandboxBuild } from '../components/SandboxBuild'
import { AddonsProvider, actionTarget, useAddon, useOpenSettings } from '../shell/AddonsContext'
import { AppShell } from '../shell/AppShell'
import { Settings } from '../views/Settings'
import { forbiddenIn } from '../test/copyRules'
import { addonList, addonsReady, addonsWith, bodyOf, headerOf, healthy, mockFetch, modelsIdle, serverInfo } from '../test/fetch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(async () => 'step-up-token'),
}))

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
  'GET /api/settings/startup': { body: { supported: true, enabled: false } },
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
      // Only a missing image has something to press: Build. Docker's own fixes are text and a link.
      const buttons = within(analysis).queryAllByRole('button').map((b) => b.textContent)
      expect(buttons).toEqual(state === 'needs_image' ? ['Build the sandbox'] : [])
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

  it('chat and image problems lead to their model screens', () => {
    for (const state of ['not_set_up', 'error']) {
      const [chat, , images] = addonList({ chat: state, images: state }) as Addon[]
      expect(actionTarget(chat)).toBe('settings-chat')
      expect(actionTarget(images)).toBe('settings-images')
    }
    const [offline] = addonList({ chat: 'offline' }) as Addon[]
    expect(actionTarget(offline)).toBe('settings-chat')
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


const DOCKER_PAGE = 'https://www.docker.com/products/docker-desktop/'
const building = (step: 'sandbox' | 'repo' | null) => ({ build: { state: 'building', step, reason: null } })
const failed = (reason: string) => ({ build: { state: 'failed', step: null, reason } })
const idle = { body: { build: { state: 'idle', step: null, reason: null } } }

async function findCard(id: string): Promise<HTMLElement> {
  await waitFor(() => expect(card(id)).not.toBeNull())
  return card(id)
}

describe('Building the analysis sandbox', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.mocked(stepUp).mockClear()
  })

  it('Analysis card offers Build when the image is missing', async () => {
    const { calls } = mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'needs_image' }),
      'GET /api/addons/analysis/build': idle,
      'POST /api/addons/analysis/build': { status: 202, body: building('sandbox') },
    })
    render(<AddonsPanel />)
    const analysis = await findCard('analysis')
    expect(await within(analysis).findByText('This takes a few minutes the first time.')).toBeInTheDocument()
    fireEvent.click(within(analysis).getByRole('button', { name: 'Build the sandbox' }))
    expect(await within(analysis).findByText('Building the sandbox, step 1 of 2.')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledWith('settings')
    const post = calls.find((c) => c.init.method === 'POST' && c.path === '/api/addons/analysis/build')!
    expect(bodyOf(post)).toEqual({ step_up_token: 'step-up-token' })
    expect(headerOf(post, 'X-Hearthkeep')).toBe('1')
    expect(within(analysis).queryByRole('button', { name: 'Build the sandbox' })).toBeNull()
  })

  it('Docker stopped adds nothing to press: the panel has Check again', async () => {
    mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'docker_stopped' }),
      'GET /api/addons/analysis/build': idle,
    })
    render(<AddonsPanel />)
    const analysis = await findCard('analysis')
    expect(within(analysis).queryByRole('link')).toBeNull()
    expect(within(analysis).queryByRole('button')).toBeNull()
  })

  it('Docker missing links the official page', async () => {
    mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'not_set_up' }),
      'GET /api/addons/analysis/build': idle,
    })
    render(<AddonsPanel />)
    const analysis = await findCard('analysis')
    const link = within(analysis).getByRole('link', { name: 'Get Docker Desktop' })
    expect(link).toHaveAttribute('href', DOCKER_PAGE)
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
    expect(within(analysis).getByText(/then Check again/)).toBeInTheDocument()
  })

  it('a build that finds Docker stopped says so, and Try again goes back to Build', async () => {
    mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'needs_image' }),
      'GET /api/addons/analysis/build': idle,
      'POST /api/addons/analysis/build': { status: 202, body: failed('docker_stopped') },
    })
    render(<AddonsPanel />)
    const analysis = await findCard('analysis')
    fireEvent.click(await within(analysis).findByRole('button', { name: 'Build the sandbox' }))
    expect(await within(analysis).findByText('Start Docker Desktop, then Check again.')).toBeInTheDocument()
    fireEvent.click(within(analysis).getByRole('button', { name: 'Try again' }))
    expect(within(analysis).getByRole('button', { name: 'Build the sandbox' })).toBeInTheDocument()
  })

  it('failed build offers Retry', async () => {
    let posts = 0
    const { calls } = mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'needs_image' }),
      'GET /api/addons/analysis/build': idle,
      'POST /api/addons/analysis/build': () => {
        posts += 1
        return { status: 202, body: posts === 1 ? failed('build_failed') : building('sandbox') }
      },
    })
    render(<AddonsPanel />)
    const analysis = await findCard('analysis')
    fireEvent.click(await within(analysis).findByRole('button', { name: 'Build the sandbox' }))
    expect(await within(analysis).findByRole('alert')).toHaveTextContent("The sandbox didn't build.")
    fireEvent.click(within(analysis).getByRole('button', { name: 'Retry' }))
    expect(await within(analysis).findByText('Building the sandbox, step 1 of 2.')).toBeInTheDocument()
    expect(calls.filter((c) => c.init.method === 'POST' && c.path === '/api/addons/analysis/build')).toHaveLength(2)
  })

  it('a start that is refused (no passkey, or the server) says so and can be tried again', async () => {
    vi.mocked(stepUp).mockRejectedValueOnce(new Error('cancelled'))
    mockFetch({
      'GET /api/addons': addonsWith({ analysis: 'needs_image' }),
      'GET /api/addons/analysis/build': idle,
      'POST /api/addons/analysis/build': { status: 202, body: building('sandbox') },
    })
    render(<AddonsPanel />)
    const analysis = await findCard('analysis')
    fireEvent.click(await within(analysis).findByRole('button', { name: 'Build the sandbox' }))
    expect(await within(analysis).findByRole('alert')).toHaveTextContent("Couldn't start the build. Try again.")
    fireEvent.click(within(analysis).getByRole('button', { name: 'Build the sandbox' }))
    expect(await within(analysis).findByText('Building the sandbox, step 1 of 2.')).toBeInTheDocument()
  })

  it('polls every 2 seconds while building, shows step 2, and stops when it is done', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    let now: object = { state: 'building', step: 'sandbox', reason: null }
    const api = { getBuild: vi.fn(async () => now as never), startBuild: vi.fn() }
    const finished = vi.fn()
    render(<SandboxBuild api={api} onFinished={finished} />)
    expect(await screen.findByText('Building the sandbox, step 1 of 2.')).toBeInTheDocument()
    const base = api.getBuild.mock.calls.length
    now = { state: 'building', step: 'repo', reason: null }
    await vi.advanceTimersByTimeAsync(2000)
    expect(await screen.findByText('Building the sandbox, step 2 of 2.')).toBeInTheDocument()
    expect(api.getBuild.mock.calls.length).toBe(base + 1)
    now = { state: 'done', step: null, reason: null }
    await vi.advanceTimersByTimeAsync(2000)
    expect(await screen.findByText('The sandbox is ready.')).toBeInTheDocument()
    expect(finished).toHaveBeenCalledTimes(1)
    const after = api.getBuild.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.getBuild.mock.calls.length).toBe(after) // no more polling once it ended
  })

  it('pauses polling while the page is hidden and stops when it unmounts', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const api = {
      getBuild: vi.fn(async () => ({ state: 'building', step: 'sandbox', reason: null }) as never),
      startBuild: vi.fn(),
    }
    const { unmount } = render(<SandboxBuild api={api} />)
    await screen.findByText('Building the sandbox, step 1 of 2.')
    const base = api.getBuild.mock.calls.length
    const visibility = vi.spyOn(document, 'visibilityState', 'get')
    visibility.mockReturnValue('hidden')
    await vi.advanceTimersByTimeAsync(6000)
    expect(api.getBuild.mock.calls.length).toBe(base)
    visibility.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(api.getBuild.mock.calls.length).toBe(base + 1)
    await vi.advanceTimersByTimeAsync(2000)
    expect(api.getBuild.mock.calls.length).toBe(base + 2)
    unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(api.getBuild.mock.calls.length).toBe(base + 2)
    visibility.mockRestore()
  })

  it('takes any API: the wizard passes its own, with no passkey step', async () => {
    const api = {
      getBuild: vi.fn(async () => ({ state: 'idle', step: null, reason: null }) as never),
      startBuild: vi.fn(async () => ({ state: 'building', step: 'repo', reason: null }) as never),
    }
    const onCheckAgain = vi.fn()
    const { rerender } = render(<SandboxBuild api={api} docker="missing" onCheckAgain={onCheckAgain} />)
    expect(screen.getByRole('link', { name: 'Get Docker Desktop' })).toHaveAttribute('href', DOCKER_PAGE)
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    expect(onCheckAgain).toHaveBeenCalledTimes(1)
    rerender(<SandboxBuild api={api} docker="ready" onCheckAgain={onCheckAgain} />) // Docker is there now
    fireEvent.click(await screen.findByRole('button', { name: 'Build the sandbox' }))
    expect(await screen.findByText('Building the sandbox, step 2 of 2.')).toBeInTheDocument()
    expect(api.startBuild).toHaveBeenCalledTimes(1)
    expect(stepUp).not.toHaveBeenCalled()
  })

  it('every view of the build keeps to the copy rules', async () => {
    const started = [
      { state: 'building', step: 'sandbox', reason: null },
      { state: 'done', step: null, reason: null },
      { state: 'failed', step: null, reason: 'docker_missing' },
      { state: 'failed', step: null, reason: 'docker_stopped' },
      { state: 'failed', step: null, reason: 'build_failed' },
    ]
    for (const build of started) {
      const api = {
        getBuild: vi.fn(async () => ({ state: 'idle', step: null, reason: null }) as never),
        startBuild: vi.fn(async () => build as never),
      }
      const { container, unmount } = render(<SandboxBuild api={api} onCheckAgain={() => {}} />)
      expect(forbiddenIn(container.textContent ?? '')).toEqual([])
      fireEvent.click(await screen.findByRole('button', { name: 'Build the sandbox' }))
      await waitFor(() => expect(api.startBuild).toHaveBeenCalled())
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Build the sandbox' })).toBeNull())
      expect(container.textContent).not.toEqual('')
      expect(forbiddenIn(container.textContent ?? '')).toEqual([])
      unmount()
    }
    for (const docker of ['missing', 'stopped'] as const) {
      const api = { getBuild: vi.fn(async () => ({ state: 'idle', step: null, reason: null }) as never), startBuild: vi.fn() }
      const { container, unmount } = render(<SandboxBuild api={api} docker={docker} onCheckAgain={() => {}} />)
      expect(container.textContent).not.toEqual('')
      expect(forbiddenIn(container.textContent ?? '')).toEqual([])
      unmount()
    }
  })
})
