import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { page, serverSession, waitForRestart, waitForSetupDone } from '../api/server'
import { stepUp } from '../api/webauthn'
import { ServerProvider } from '../components/RestartNotice'
import { ServerPanel } from '../components/ServerPanel'
import { UpdateNote } from '../components/UpdateNote'
import { renderWithAddons } from '../test/addons'
import { forbiddenIn } from '../test/copyRules'
import { bodyOf, headerOf, mockFetch, modelsIdle, serverInfo, type Handler, type Reply } from '../test/fetch'
import { Settings } from '../views/Settings'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const NEEDED = 'A change needs Hearthkeep to restart.'
const LATER = '2026-11-04T09:30:00+00:00'
const STOPPED = 'Hearthkeep has stopped. Open it again from the Start menu on the PC.'

let reload: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('step-token')
  reload = vi.spyOn(page, 'reload').mockImplementation(() => {})
  serverSession.stopped = false
})

afterEach(() => {
  reload.mockRestore()
})

/** GET /api/server answers `first`, then the restarted server's `started_at`. */
function restartingServer(first: Reply = serverInfo()): Handler {
  let asked = 0
  return () => (++asked === 1 ? first : serverInfo({ started_at: LATER }))
}

function settingsRoutes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'GET /api/devices': { body: [] },
    'GET /api/folders': { body: { folders: [], suggestions: [] } },
    'GET /api/settings/startup': { body: { supported: true, enabled: true, command: null } },
    ...modelsIdle,
    ...extra,
  })
}

describe('Settings › General', () => {
  it('restart asks for the passkey, then reloads when the server is back', async () => {
    const { calls } = mockFetch({
      'GET /api/server': restartingServer(),
      'POST /api/server/restart': { status: 202, body: { ok: true } },
    })
    render(<ServerPanel />)
    expect(screen.getByRole('heading', { level: 2, name: 'General' })).toBeInTheDocument()
    expect(await screen.findByText('Version 0.3.0')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart Hearthkeep' }))
    await waitFor(() => expect(reload).toHaveBeenCalledOnce())
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('server')
    const post = calls.find((c) => c.path === '/api/server/restart')!
    expect(bodyOf(post)).toEqual({ step_up_token: 'step-token' })
    expect(headerOf(post, 'X-Hearthkeep')).toBe('1')
    expect(calls.filter((c) => c.path === '/api/server')).toHaveLength(2)
  })

  it('restart is hidden when Hearthkeep is not supervised', async () => {
    mockFetch({ 'GET /api/server': serverInfo({ supervised: false, restart_needed: true }) })
    render(<ServerPanel />)
    expect(await screen.findByText('Version 0.3.0')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart Hearthkeep' })).toBeNull()
    expect(screen.queryByText(NEEDED)).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop Hearthkeep' })).toBeInTheDocument()
  })

  it('stop confirms, steps up, and says how to start again', async () => {
    const { calls } = mockFetch({
      'GET /api/server': serverInfo(),
      'POST /api/server/stop': { status: 202, body: { ok: true } },
    })
    render(<ServerPanel />)
    await screen.findByText('Version 0.3.0')
    fireEvent.click(screen.getByRole('button', { name: 'Stop Hearthkeep' }))
    const dialog = screen.getByRole('dialog', { name: 'Stop Hearthkeep?' })
    expect(dialog).toHaveTextContent(
      'It stops on this PC and on your other devices until you open it again from the Start menu on the PC.',
    )
    expect(stepUp).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(calls.some((c) => c.path === '/api/server/stop')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Stop Hearthkeep' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Stop' }))
    const stopped = await screen.findByText('Hearthkeep has stopped. Open it again from the Start menu on the PC.')
    expect(stopped.closest('[role="status"]')).not.toBeNull()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('server')
    expect(bodyOf(calls.find((c) => c.path === '/api/server/stop')!)).toEqual({ step_up_token: 'step-token' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop Hearthkeep' })).toBeNull()
  })

  it('restart notice shows in Settings › Add-ons and General only when needed', async () => {
    settingsRoutes({ 'GET /api/server': serverInfo({ restart_needed: true, restart_reasons: ['sandbox'] }) })
    const needed = render(<Settings />)
    await waitFor(() => expect(screen.getAllByText(NEEDED)).toHaveLength(2))
    const addons = needed.container.querySelector('#settings-addons') as HTMLElement
    const general = needed.container.querySelector('#settings-general') as HTMLElement
    expect(within(addons).getByText(NEEDED)).toBeInTheDocument()
    expect(within(addons).getByRole('button', { name: 'Restart now' })).toBeInTheDocument()
    expect(within(general).getByText(NEEDED)).toBeInTheDocument()
    needed.unmount()

    settingsRoutes({ 'GET /api/server': serverInfo() })
    render(<Settings />)
    await screen.findByText('Version 0.3.0')
    expect(screen.queryByText(NEEDED)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Restart now' })).toBeNull()
  })

  it('restart now in the notice restarts and reloads', async () => {
    const { calls } = mockFetch({
      'GET /api/server': restartingServer(serverInfo({ restart_needed: true })),
      'POST /api/server/restart': { status: 202, body: { ok: true } },
    })
    render(<ServerPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart now' }))
    await waitFor(() => expect(reload).toHaveBeenCalledOnce())
    expect(calls.filter((c) => c.path === '/api/server/restart')).toHaveLength(1)
  })

  it('a step-up refusal shows a plain message and keeps running', async () => {
    mockFetch({
      'GET /api/server': serverInfo(),
      'POST /api/server/restart': { status: 403, body: { reason: 'step_up_required' } },
    })
    render(<ServerPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart Hearthkeep' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Your passkey couldn't be confirmed. Try again.")
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Restart Hearthkeep' })).not.toHaveAttribute('aria-busy')
  })

  it('a cancelled passkey or a server that cannot restart itself says so', async () => {
    vi.mocked(stepUp).mockRejectedValueOnce(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    mockFetch({
      'GET /api/server': serverInfo(),
      'POST /api/server/restart': { status: 409, body: { reason: 'not_supervised' } },
    })
    render(<ServerPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart Hearthkeep' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Restart was cancelled.')
    fireEvent.click(screen.getByRole('button', { name: 'Restart Hearthkeep' }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        "Hearthkeep can't restart itself here. Stop it, then open it from the Start menu.",
      ),
    )
    expect(reload).not.toHaveBeenCalled()
  })

  it('a restart that never comes back says so, and the buttons work again', async () => {
    mockFetch({
      'GET /api/server': serverInfo(),
      'POST /api/server/restart': { status: 202, body: { ok: true } },
    })
    render(
      <ServerProvider wait={{ timeoutMs: 30, intervalMs: 5 }}>
        <ServerPanel />
      </ServerProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Restart Hearthkeep' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Hearthkeep didn't come back. Open it from the Start menu on the PC.",
    )
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Restart Hearthkeep' })).not.toHaveAttribute('aria-busy')
    expect(screen.getByRole('button', { name: 'Stop Hearthkeep' })).toBeEnabled()
  })

  it('after a stop the focus moves to General and the message is announced', async () => {
    mockFetch({
      'GET /api/server': serverInfo(),
      'POST /api/server/stop': { status: 202, body: { ok: true } },
    })
    render(<ServerPanel />)
    await screen.findByText('Version 0.3.0')
    const section = document.getElementById('settings-general')!
    const live = section.querySelector(':scope > [role="status"]') as HTMLElement
    expect(live).toHaveAttribute('aria-live', 'polite')
    expect(live).toHaveTextContent('')
    fireEvent.click(screen.getByRole('button', { name: 'Stop Hearthkeep' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(live).toHaveTextContent(STOPPED))
    expect(section.querySelector(':scope > [role="status"]')).toBe(live)
    await waitFor(() => expect(section).toHaveFocus())
  })

  it('stopped stays said after leaving Settings and coming back', async () => {
    settingsRoutes({
      'GET /api/server': serverInfo(),
      'POST /api/server/stop': { status: 202, body: { ok: true } },
    })
    const first = render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Stop Hearthkeep' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Stop' }))
    await screen.findByText(STOPPED)
    first.unmount()

    // The server is gone now: the page still says why, with nothing to press.
    settingsRoutes({ 'GET /api/server': () => Promise.reject(new TypeError('Failed to fetch')) })
    render(<Settings />)
    expect(await screen.findByText(STOPPED)).toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByText("Couldn't check Hearthkeep's version.")).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop Hearthkeep' })).toBeNull()
  })

  it('Settings asks the server once for both panels', async () => {
    const { calls } = settingsRoutes({ 'GET /api/server': serverInfo({ restart_needed: true }) })
    render(<Settings />)
    await waitFor(() => expect(screen.getAllByText(NEEDED)).toHaveLength(2))
    expect(calls.filter((c) => c.path === '/api/server')).toHaveLength(1)
  })

  it('a restart from Add-ons shows its error there only', async () => {
    settingsRoutes({
      'GET /api/server': serverInfo({ restart_needed: true }),
      'POST /api/server/restart': { status: 403, body: { reason: 'step_up_required' } },
    })
    const { container } = render(<Settings />)
    await waitFor(() => expect(screen.getAllByText(NEEDED)).toHaveLength(2))
    const addons = container.querySelector('#settings-addons') as HTMLElement
    fireEvent.click(within(addons).getByRole('button', { name: 'Restart now' }))
    await waitFor(() =>
      expect(within(addons).getByText("Your passkey couldn't be confirmed. Try again.")).toBeInTheDocument(),
    )
    const general = container.querySelector('#settings-general') as HTMLElement
    expect(within(general).queryByText("Your passkey couldn't be confirmed. Try again.")).toBeNull()
  })

  it('Settings can be opened at General', async () => {
    settingsRoutes({ 'GET /api/server': serverInfo() })
    render(<Settings focus="settings-general" />)
    await waitFor(() => expect(document.activeElement?.id).toBe('settings-general'))
  })

  it('never shows a command or placeholder', async () => {
    mockFetch({
      'GET /api/server': serverInfo({ restart_needed: true }),
      'POST /api/server/restart': { status: 409, body: { reason: 'not_supervised' } },
    })
    const { container } = render(<ServerPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Restart Hearthkeep' }))
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Stop Hearthkeep' }))
    const text = (container.textContent ?? '') + (screen.getByRole('dialog').textContent ?? '')
    expect(forbiddenIn(text)).toEqual([])
  })
})

describe('waiting for the server', () => {
  it('waitForRestart tolerates a server that is away and sees the new start', async () => {
    let asked = 0
    mockFetch({
      'GET /api/server': () => {
        asked += 1
        if (asked === 1) return serverInfo()
        if (asked === 2) return Promise.reject(new TypeError('Failed to fetch'))
        return serverInfo({ started_at: LATER })
      },
    })
    expect(await waitForRestart('2026-11-04T08:00:00+00:00', { intervalMs: 1 })).toBe(true)
    expect(asked).toBe(3)
  })

  it('waitForRestart gives up when the server never comes back', async () => {
    mockFetch({ 'GET /api/server': () => Promise.reject(new TypeError('Failed to fetch')) })
    expect(await waitForRestart('x', { timeoutMs: 30, intervalMs: 5 })).toBe(false)
  })

  it('a check that hangs is given up on, and the wait goes on', async () => {
    let asked = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init: RequestInit = {}) => {
        asked += 1
        if (asked === 1) {
          return new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => reject(init.signal?.reason))
          })
        }
        return Promise.resolve(
          new Response(JSON.stringify({ status: 'ok', setup_mode: false }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }),
    )
    expect(await waitForSetupDone({ timeoutMs: 2000, intervalMs: 10 })).toBe(true)
    expect(asked).toBe(2)
  })

  it('waitForSetupDone waits through the hand-over to the real server', async () => {
    let asked = 0
    mockFetch({
      'GET /api/health': () => {
        asked += 1
        if (asked === 1) return { body: { status: 'ok', setup_mode: true } }
        if (asked === 2) return Promise.reject(new TypeError('Failed to fetch'))
        if (asked === 3) return { status: 502, body: {} }
        return { body: { status: 'ok', setup_mode: false } }
      },
    })
    expect(await waitForSetupDone({ intervalMs: 1 })).toBe(true)
    expect(asked).toBe(4)
  })
})

describe('Update note', () => {
  const note = {
    version: '0.3.0',
    items: [
      { id: 'startup', text: 'Start at login now uses the new launcher.', addon: null },
      { id: 'sandbox_image', text: 'Analysis needs its sandbox rebuilt. Check it in Settings › Add-ons.', addon: 'analysis' },
    ],
  }

  it('update note lists items with a button to the add-on and dismisses', async () => {
    const { calls } = mockFetch({
      'GET /api/update-note': { body: { note } },
      'POST /api/update-note/dismiss': { body: { ok: true } },
    })
    const { openSettings, container } = renderWithAddons(<UpdateNote />, [])
    const section = await screen.findByRole('region', { name: 'Hearthkeep was updated' })
    expect(within(section).getByText(note.items[0].text)).toBeInTheDocument()
    expect(within(section).getByText(note.items[1].text)).toBeInTheDocument()
    const open = within(section).getAllByRole('button', { name: 'Open Settings › Add-ons' })
    expect(open).toHaveLength(1)
    expect(open[0]).toHaveAccessibleDescription(note.items[1].text)
    fireEvent.click(open[0])
    expect(openSettings).toHaveBeenCalledExactlyOnceWith('addon-analysis')
    expect(forbiddenIn(container.textContent ?? '')).toEqual([])

    fireEvent.click(within(section).getByRole('button', { name: 'Got it' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Hearthkeep was updated' })).toBeNull())
    const dismiss = calls.find((c) => c.path === '/api/update-note/dismiss')!
    expect(dismiss.init.method).toBe('POST')
    expect(headerOf(dismiss, 'X-Hearthkeep')).toBe('1')
  })

  it('update note with no items says nothing needs you', async () => {
    mockFetch({ 'GET /api/update-note': { body: { note: { version: '0.3.0', items: [] } } } })
    renderWithAddons(<UpdateNote />, [])
    const section = await screen.findByRole('region', { name: 'Hearthkeep was updated' })
    expect(within(section).getByText('Nothing needs you.')).toBeInTheDocument()
    expect(within(section).queryByRole('button', { name: 'Open Settings › Add-ons' })).toBeNull()
  })

  it('renders nothing without a note or when it cannot load', async () => {
    const { calls } = mockFetch({ 'GET /api/update-note': { body: { note: null } } })
    const { container, unmount } = renderWithAddons(<UpdateNote />, [])
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(container).toBeEmptyDOMElement()
    unmount()

    mockFetch({ 'GET /api/update-note': { status: 500, body: {} } })
    const failed = renderWithAddons(<UpdateNote />, [])
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(failed.container).toBeEmptyDOMElement()
  })
})
