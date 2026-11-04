import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { startRegistration } from '@simplewebauthn/browser'
import App from '../App'
import { page, waitForSetupDone } from '../api/server'
import type { Catalog, CatalogItem, Job } from '../api/models'
import { readSetupSecret } from '../api/setup'
import { forbiddenIn } from '../test/copyRules'
import { addonList, bodyOf, healthy, mockFetch, signedIn, signedOut, type Handler, type Reply } from '../test/fetch'
import { SetupPage } from '../views/SetupPage'
import { SETUP_COPY } from '../views/setup/copy'

vi.mock('@simplewebauthn/browser', () => ({
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
  browserSupportsWebAuthn: () => true,
}))

vi.mock('../api/server', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/server')>()
  return { ...actual, waitForSetupDone: vi.fn(actual.waitForSetupDone) }
})

const SECRET = 'a'.repeat(43)
const CODE = 'ABCDEFGH'
const registerOptions = { challenge: 'cmVnaXN0ZXI', user: { id: 'b3duZXI', name: 'owner', displayName: 'Owner' } }
const attestation = { id: 'cred-1', rawId: 'cred-1', type: 'public-key', response: {}, clientExtensionResults: {} }
const BUSY = 'Busy for a moment, trying again…'
const ENDED = 'This setup link has ended. Double-click Setup.cmd in the Hearthkeep folder to start again.'

function setupRoutes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'POST /api/setup/status': { body: { state: 'needs_passkey' } },
    'POST /api/setup/enrollment-code': { body: { code: CODE, expires_in_minutes: 15 } },
    'POST /api/auth/enroll/options': { body: { challenge_id: 'ch-1', options: registerOptions } },
    'POST /api/auth/enroll/verify': signedIn,
    'GET /api/health': { body: { ...healthy.body, setup_mode: false } },
    ...extra,
  })
}

let replace: ReturnType<typeof vi.spyOn>

/** Done: "Open Hearthkeep" goes to the app. */
async function openApp() {
  fireEvent.click(await screen.findByRole('button', { name: 'Open Hearthkeep' }))
  await waitFor(() => expect(replace).toHaveBeenCalledWith('/'))
}

/** The 2a tests start on the passkey step, as a wizard walked to the end would. */
const PASSKEY_STEP = '5'

beforeEach(() => {
  window.sessionStorage.setItem('hk.setup.step', PASSKEY_STEP)
  window.history.replaceState(null, '', `/setup#${SECRET}`)
  vi.mocked(startRegistration).mockReset().mockResolvedValue(attestation as never)
  replace = vi.spyOn(page, 'replace').mockImplementation(() => {})
})

afterEach(() => {
  window.sessionStorage.clear()
  replace.mockRestore()
  window.history.replaceState(null, '', '/')
})

describe('Setup page', () => {
  it('reads the secret, clears it from the address bar, and creates the passkey with the fetched code', async () => {
    const logged = [vi.spyOn(console, 'log'), vi.spyOn(console, 'info'), vi.spyOn(console, 'warn'), vi.spyOn(console, 'error')]
    const { calls } = setupRoutes()
    render(<App />)
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/setup')

    let finish: (done: boolean) => void = () => {}
    vi.mocked(waitForSetupDone).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)))
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText('Finishing setup…')).toHaveAttribute('role', 'status')
    finish(true)
    await openApp()

    const status = calls.find((c) => c.path === '/api/setup/status')!
    expect(bodyOf(status)).toEqual({ secret: SECRET })
    const code = calls.find((c) => c.path === '/api/setup/enrollment-code')!
    expect(bodyOf(code)).toEqual({ secret: SECRET })
    expect(code.init.method).toBe('POST')
    const options = calls.find((c) => c.path === '/api/auth/enroll/options')!
    expect(bodyOf(options)).toEqual({ code: CODE })
    expect(bodyOf(calls.find((c) => c.path === '/api/auth/enroll/verify')!)).toMatchObject({ device_name: expect.any(String) })
    // The session check waits until setup is over.
    expect(calls.some((c) => c.path === '/api/auth/session')).toBe(false)

    for (const call of calls) {
      expect(call.path).not.toContain(SECRET)
      expect(call.path).not.toContain(CODE)
    }
    expect(document.body.innerHTML).not.toContain(CODE)
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(window.location.href).not.toContain(SECRET)
    expect(JSON.stringify({ ...window.localStorage, ...window.sessionStorage })).not.toContain(SECRET)
    for (const spy of logged) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(SECRET)
      expect(JSON.stringify(spy.mock.calls)).not.toContain(CODE)
      spy.mockRestore()
    }
  })

  it('reads the secret once under strict mode', async () => {
    setupRoutes()
    render(
      <StrictMode>
        <SetupPage onLeave={() => {}} />
      </StrictMode>,
    )
    expect(await screen.findByRole('button', { name: 'Create passkey' })).toBeInTheDocument()
  })

  it('without a secret it says to double-click Setup.cmd', () => {
    window.history.replaceState(null, '', '/setup')
    const { calls } = setupRoutes()
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: 'Set up Hearthkeep' })).toBeInTheDocument()
    expect(
      screen.getByText('Open setup from this PC: double-click Setup.cmd in the Hearthkeep folder.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull()
    expect(calls).toHaveLength(0)
  })

  it('drops a fragment that is not a secret, and clears it too', () => {
    window.history.replaceState(null, '', '/setup#short')
    expect(readSetupSecret()).toBeNull()
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/setup')
  })

  it('a 404 status leaves for the normal app', async () => {
    setupRoutes({
      'POST /api/setup/status': { status: 404, body: { reason: 'not_found' } },
      'GET /api/auth/session': signedOut,
      'GET /api/health': healthy,
    })
    render(<App />)
    expect(await screen.findByRole('button', { name: 'Sign in with passkey' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
  })

  it('a 403 says the link has ended', async () => {
    setupRoutes({ 'POST /api/setup/status': { status: 403, body: { reason: 'setup_secret' } } })
    render(<SetupPage onLeave={() => {}} />)
    expect(await screen.findByText(ENDED)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull()
  })

  it('a 429 waits and tries again instead of giving up', async () => {
    let asked = 0
    const { calls } = setupRoutes({
      'POST /api/setup/status': () => (++asked === 1 ? { status: 429, body: { reason: 'rate_limited' } } : { body: { state: 'needs_passkey' } }),
      'POST /api/setup/enrollment-code': () =>
        ++asked === 3 ? { status: 429, body: { reason: 'rate_limited' } } : { body: { code: CODE, expires_in_minutes: 15 } },
    })
    render(<SetupPage onLeave={() => {}} retryMs={40} />)
    expect(await screen.findByText(BUSY)).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText(BUSY)).toBeInTheDocument()
    await openApp()
    expect(screen.queryByText(ENDED)).toBeNull()
    expect(calls.filter((c) => c.path === '/api/setup/enrollment-code')).toHaveLength(2)
  })

  it('a cancelled passkey shows the message and the button again', async () => {
    vi.mocked(startRegistration).mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    setupRoutes()
    render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Setup was cancelled.')
    expect(screen.getByRole('button', { name: 'Create passkey' })).toBeEnabled()
    expect(replace).not.toHaveBeenCalled()
  })

  it('says when Hearthkeep is slow to start after setup', async () => {
    vi.mocked(waitForSetupDone).mockResolvedValueOnce(false)
    setupRoutes()
    render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(
      await screen.findByText('Hearthkeep is taking a while to start. Open it from the Start menu in a minute.'),
    ).toBeInTheDocument()
    expect(replace).not.toHaveBeenCalled()
  })

  it('a setup link opened again in this tab is taken, cleared and checked', async () => {
    const { calls } = setupRoutes()
    render(<SetupPage onLeave={() => {}} />)
    await screen.findByRole('button', { name: 'Create passkey' })
    const NEXT = 'b'.repeat(40)
    window.location.hash = `#${NEXT}`
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await waitFor(() => expect(calls.filter((c) => c.path === '/api/setup/status')).toHaveLength(2))
    expect(window.location.hash).toBe('')
    expect(bodyOf(calls.filter((c) => c.path === '/api/setup/status')[1])).toEqual({ secret: NEXT })
    expect(document.body.innerHTML).not.toContain(NEXT)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    await openApp()
    expect(bodyOf(calls.find((c) => c.path === '/api/setup/enrollment-code')!)).toEqual({ secret: NEXT })
  })

  it("when the setup server can't be reached it says so and tries again", async () => {
    let up = false
    setupRoutes({
      'POST /api/setup/status': () =>
        up ? { body: { state: 'needs_passkey' } } : Promise.reject(new TypeError('Failed to fetch')),
    })
    render(<SetupPage onLeave={() => {}} />)
    expect(
      await screen.findByText("Can't reach Hearthkeep. Check that the setup window is still open on this PC."),
    ).toBeInTheDocument()
    up = true
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('button', { name: 'Create passkey' })).toBeInTheDocument()
  })

  it('a 404 for the code leaves for the normal app', async () => {
    const onLeave = vi.fn()
    setupRoutes({ 'POST /api/setup/enrollment-code': { status: 404, body: { reason: 'not_found' } } })
    render(<SetupPage onLeave={onLeave} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    await waitFor(() => expect(onLeave).toHaveBeenCalledOnce())
    expect(startRegistration).not.toHaveBeenCalled()
  })

  it('a 403 for the code says the link has ended', async () => {
    setupRoutes({ 'POST /api/setup/enrollment-code': { status: 403, body: { reason: 'setup_secret' } } })
    render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await screen.findByText(ENDED)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create passkey' })).toBeNull()
    expect(startRegistration).not.toHaveBeenCalled()
  })

  it("the copy rules report every forbidden piece, with the server's flags", () => {
    expect(forbiddenIn('Run hearthkeep setup, then Hearthkeep open, then uv run x --data <data>')).toEqual([
      'hearthkeep setup',
      'Hearthkeep open',
      'uv run',
      '--data',
      '<data>',
    ])
    // The placeholder rule is case-sensitive, as on the server.
    expect(forbiddenIn('UV RUN')).toEqual([])
    expect(forbiddenIn('Set up Hearthkeep. Open it from the Start menu.')).toEqual([])
  })

  it('never shows a command or placeholder', async () => {
    const seen: string[] = []
    // No secret.
    window.history.replaceState(null, '', '/setup')
    const first = render(<SetupPage onLeave={() => {}} />)
    seen.push(first.container.textContent ?? '')
    first.unmount()
    // The link has ended.
    window.history.replaceState(null, '', `/setup#${SECRET}`)
    setupRoutes({ 'POST /api/setup/status': { status: 403, body: { reason: 'setup_secret' } } })
    const ended = render(<SetupPage onLeave={() => {}} />)
    await screen.findByText(ENDED)
    seen.push(ended.container.textContent ?? '')
    ended.unmount()
    // Ready, a cancelled passkey, then slow to start.
    window.history.replaceState(null, '', `/setup#${SECRET}`)
    vi.mocked(startRegistration).mockRejectedValueOnce(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    vi.mocked(waitForSetupDone).mockResolvedValueOnce(false)
    setupRoutes()
    const ready = render(<SetupPage onLeave={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    await screen.findByRole('alert')
    seen.push(ready.container.textContent ?? '')
    fireEvent.click(screen.getByRole('button', { name: 'Create passkey' }))
    await screen.findByText(/taking a while/)
    seen.push(ready.container.textContent ?? '')

    for (const text of seen) {
      expect(forbiddenIn(text)).toEqual([])
    }
  })
})

// ---- the wizard ---------------------------------------------------------------------------

const RTX = { gpu_name: 'NVIDIA GeForce RTX 4080', vram_gb: 16, nvidia: true, ram_gb: 32, free_disk_gb: 412 }
const HOME = 'C:\\Hearthkeep\\data'
const PC = { pc: RTX, data_dir: HOME, data_dir_default: true, can_move_data: true }

function catalogItem(over: Partial<CatalogItem> & Pick<CatalogItem, 'id' | 'kind' | 'name'>): CatalogItem {
  return {
    note: '',
    licence: 'Apache 2.0',
    licence_url: `https://example.org/licences/${over.id}`,
    size_bytes: 1_000_000_000,
    needs_nvidia: false,
    variant: '',
    card: '',
    installed: false,
    fits: true,
    ...over,
  }
}

const CATALOG: Catalog = {
  pc: RTX,
  recommended: { chat: 'chat-gemma-4-12b', runtime: 'llama-cuda', reason: 'fits_gpu' },
  items: [
    catalogItem({
      id: 'llama-cuda',
      kind: 'runtime',
      name: 'Chat engine for NVIDIA graphics cards',
      size_bytes: 678_456_271,
      variant: 'cuda',
      needs_nvidia: true,
    }),
    catalogItem({ id: 'chat-qwen3.5-4b', kind: 'chat', name: 'Qwen3.5 4B', card: 'qwen3.5-4b', size_bytes: 3_013_027_808 }),
    catalogItem({ id: 'chat-gemma-4-12b', kind: 'chat', name: 'Gemma 4 12B', card: 'gemma-4-12b', size_bytes: 10_361_358_240 }),
    catalogItem({
      id: 'comfyui',
      kind: 'comfyui',
      name: 'Picture maker (ComfyUI)',
      size_bytes: 1_945_636_677,
      needs_nvidia: true,
      licence: 'GPL-3.0',
    }),
    catalogItem({
      id: 'image-z-image-turbo',
      kind: 'image',
      name: 'Z-Image Turbo',
      card: 'z-image-turbo',
      size_bytes: 20_690_152_836,
      needs_nvidia: true,
    }),
  ],
  chat: { source: 'none', model: null, base_url: null },
  runtime_ready: false,
  restart_needed: false,
  supervised: false,
}

const FRESH_ADDONS = {
  chat: 'not_set_up',
  analysis: 'needs_image',
  images: 'not_set_up',
  github: 'not_set_up',
  devices: 'this_pc_only',
  startup: 'off',
}

function addonsReply(states: Partial<Record<string, string>> = {}): Reply {
  return { body: { addons: addonList({ ...FRESH_ADDONS, ...states }), checked_at: '2026-11-04T09:00:00+00:00' } }
}

function runningJob(ids: string[], state: Job['state'] = 'running'): Job {
  return {
    id: 'job-1',
    state,
    restart_needed: false,
    items: ids.map((id) => ({
      id,
      name: CATALOG.items.find((i) => i.id === id)?.name ?? id,
      state: state === 'done' ? 'done' : 'downloading',
      done_bytes: 0,
      total_bytes: 1_000_000_000,
      speed_bps: 0,
      error: null,
    })),
  }
}

/** POST /api/setup/install starts `job`; install/status answers it from then on. */
function installRoutes(job: Job): Record<string, Handler | Reply> {
  let started = false
  return {
    'POST /api/setup/install': () => {
      started = true
      return { status: 202, body: { job } }
    },
    'POST /api/setup/install/status': () => ({ body: { job: started ? job : null } }),
  }
}

function wizardRoutes(extra: Record<string, Handler | Reply> = {}) {
  return setupRoutes({
    'POST /api/setup/pc': { body: PC },
    'POST /api/setup/catalog': { body: CATALOG },
    'POST /api/setup/addons': addonsReply(),
    'POST /api/setup/install/status': { body: { job: null } },
    'POST /api/setup/startup': (init) => ({ body: { enabled: bodyOf({ path: '', init }).enabled } }),
    'POST /api/setup/sandbox/status': { body: { build: { state: 'idle', step: null, reason: null } } },
    ...extra,
  })
}

function heading(name: string | RegExp) {
  return screen.findByRole('heading', { level: 2, name })
}

async function next(label: 'Next' | 'Skip for now' = 'Next') {
  fireEvent.click(await screen.findByRole('button', { name: label }))
}

function renderWizard() {
  return render(<SetupPage onLeave={() => {}} pollMs={20} moveWait={{ intervalMs: 10, timeoutMs: 2000 }} />)
}

describe('Setup wizard', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('walks through every step with the setup adapter', async () => {
    const started = runningJob(['llama-cuda', 'chat-gemma-4-12b'])
    const { calls } = wizardRoutes(installRoutes(started))
    render(<App />)

    expect(await heading('Welcome to Hearthkeep')).toBeInTheDocument()
    expect(screen.getByText(/about 10 to 30 minutes, mostly downloading/)).toBeInTheDocument()
    // Gemma and the CUDA engine: 10.4 + 0.7 GB.
    expect(await screen.findByText(/need about 11\.0 GB of free space/)).toBeInTheDocument()
    await next()

    expect(await heading('Your PC')).toBeInTheDocument()
    expect(await screen.findByText('NVIDIA GeForce RTX 4080, 16 GB of graphics memory')).toBeInTheDocument()
    expect(screen.getByText('32 GB')).toBeInTheDocument()
    expect(screen.getByText('412 GB')).toBeInTheDocument()
    expect(screen.getByText(HOME)).toBeInTheDocument()
    await next()

    expect(await heading('Chat model')).toBeInTheDocument()
    const gemma = (await screen.findByText('Gemma 4 12B', { selector: '.model-name' })).closest('li')!
    expect(within(gemma).getByText('Recommended for this PC')).toBeInTheDocument()
    expect(within(gemma).getByRole('link', { name: /Licence/ })).toHaveAttribute(
      'href',
      'https://example.org/licences/chat-gemma-4-12b',
    )
    expect(screen.getByRole('heading', { level: 3, name: 'Use a model server I already run' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Skip for now' })).toBeInTheDocument()
    fireEvent.click(within(gemma).getByRole('button', { name: 'Choose Gemma 4 12B' }))
    expect(within(gemma).getByRole('button', { name: 'Choose Gemma 4 12B' })).toHaveAttribute('aria-pressed', 'true')
    expect(calls.some((c) => c.path === '/api/setup/install')).toBe(false) // nothing downloads yet
    await next()

    expect(await heading('Add-ons')).toBeInTheDocument()
    await next()

    expect(await heading('Downloads')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Start downloads' }))
    expect(await screen.findByText('0 of 2 done')).toBeInTheDocument()
    await next()

    expect(await heading('Your passkey')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    expect(await heading('Hearthkeep is ready')).toBeInTheDocument()
    // The download still running when the passkey was made carries on after the hand-over.
    const still = screen.getByRole('heading', { level: 3, name: 'Still downloading' })
    expect(still.nextElementSibling).toHaveTextContent('Gemma 4 12B')
    expect(
      screen.getByText(
        "They'll carry on in a moment, when Hearthkeep starts on its own. You'll see their progress in Settings › Chat model.",
      ),
    ).toBeInTheDocument()
    await openApp()

    const setupCalls = calls.filter((c) => c.path.startsWith('/api/setup/'))
    expect(new Set(setupCalls.map((c) => c.path))).toEqual(
      new Set([
        '/api/setup/status',
        '/api/setup/catalog',
        '/api/setup/addons',
        '/api/setup/install/status',
        '/api/setup/pc',
        '/api/setup/sandbox/status',
        '/api/setup/install',
        '/api/setup/enrollment-code',
      ]),
    )
    for (const call of setupCalls) {
      expect(call.init.method).toBe('POST')
      expect(bodyOf(call).secret).toBe(SECRET)
    }
    for (const call of calls) expect(call.path).not.toContain(SECRET)
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(JSON.stringify({ ...window.sessionStorage, ...window.localStorage })).not.toContain(SECRET)
  })

  it('a reload keeps the step and not the secret', async () => {
    wizardRoutes()
    const first = renderWizard()
    await heading('Welcome to Hearthkeep')
    await next()
    await heading('Your PC')
    await next()
    await heading('Chat model')
    first.unmount()
    expect(window.sessionStorage.getItem('hk.setup.step')).toBe('2')
    expect(JSON.stringify({ ...window.sessionStorage, ...window.localStorage })).not.toContain(SECRET)

    // A reload: the fragment is gone, so setup asks for the link again.
    window.history.replaceState(null, '', '/setup')
    const reloaded = renderWizard()
    expect(
      screen.getByText('Open setup from this PC: double-click Setup.cmd in the Hearthkeep folder.'),
    ).toBeInTheDocument()
    reloaded.unmount()
    // Opened again from Setup.cmd in this tab: back on the same step.
    window.history.replaceState(null, '', `/setup#${SECRET}`)
    renderWizard()
    expect(await heading('Chat model')).toBeInTheDocument()
  })

  it('moving the data folder waits for the server and carries on', async () => {
    window.sessionStorage.setItem('hk.setup.step', '1')
    const TARGET = 'D:\\Hearthkeep'
    let moved = false
    let healthAsks = 0
    const { calls } = wizardRoutes({
      'POST /api/setup/pc': () => ({ body: moved ? { ...PC, data_dir: TARGET, data_dir_default: false } : PC }),
      'POST /api/setup/data-folder': () => {
        moved = true
        return { status: 202, body: { ok: true } }
      },
      'GET /api/health': () =>
        ++healthAsks === 1
          ? Promise.reject(new TypeError('Failed to fetch'))
          : { body: { ...healthy.body, setup_mode: true } },
    })
    renderWizard()
    expect(await screen.findByText(HOME)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Change…' }))
    const field = screen.getByRole('textbox', { name: 'New data folder' })
    expect(field).toHaveValue(HOME)
    fireEvent.change(field, { target: { value: TARGET } })
    fireEvent.click(screen.getByRole('button', { name: 'Move here' }))
    expect(await screen.findByText('Moving the data folder…')).toHaveAttribute('role', 'status')
    expect(await screen.findByText(TARGET)).toBeInTheDocument()
    expect(screen.queryByText('Moving the data folder…')).toBeNull()
    expect(bodyOf(calls.find((c) => c.path === '/api/setup/data-folder')!)).toEqual({ secret: SECRET, path: TARGET })
    // The server was asked again with the secret this page holds.
    const statuses = calls.filter((c) => c.path === '/api/setup/status')
    expect(statuses).toHaveLength(2)
    expect(bodyOf(statuses[1])).toEqual({ secret: SECRET })
    expect(healthAsks).toBeGreaterThanOrEqual(2)
    expect(screen.getByRole('heading', { level: 2, name: 'Your PC' })).toBeInTheDocument()
  })

  it('a folder the server refuses shows its sentence, and Change… is hidden when it cannot move', async () => {
    window.sessionStorage.setItem('hk.setup.step', '1')
    const sentence = 'Choose a folder outside the one Hearthkeep uses now.'
    wizardRoutes({
      'POST /api/setup/data-folder': { status: 400, body: { reason: 'bad_folder', message: sentence } },
    })
    const first = renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: 'Change…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Move here' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(sentence)
    first.unmount()

    window.history.replaceState(null, '', `/setup#${SECRET}`)
    wizardRoutes({
      'POST /api/setup/data-folder': { status: 400, body: { reason: 'not_enough_space', free_gb: 2.5, needed_gb: 5 } },
    })
    const second = renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: 'Change…' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'New data folder' }), { target: { value: 'E:\\small' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move here' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "There isn't enough free space there: 5 GB is needed and 2.5 GB is free.",
    )
    second.unmount()

    window.history.replaceState(null, '', `/setup#${SECRET}`)
    wizardRoutes({ 'POST /api/setup/pc': { body: { ...PC, can_move_data: false } } })
    renderWizard()
    expect(await screen.findByText(HOME)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Change…' })).toBeNull()
  })

  it('downloads start once with everything chosen', async () => {
    window.sessionStorage.setItem('hk.setup.step', '2')
    const started = runningJob(['llama-cuda', 'chat-gemma-4-12b', 'comfyui', 'image-z-image-turbo'])
    const { calls } = wizardRoutes(installRoutes(started))
    renderWizard()
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Gemma 4 12B' }))
    await next()
    await heading('Add-ons')
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Z-Image Turbo' }))
    await next()
    await heading('Downloads')
    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    expect(
      within(confirm)
        .getAllByRole('listitem')
        .map((li) => li.querySelector('b')!.textContent),
    ).toEqual(['Chat engine for NVIDIA graphics cards', 'Gemma 4 12B', 'Picture maker (ComfyUI)', 'Z-Image Turbo'])
    fireEvent.click(within(confirm).getByRole('button', { name: 'Start downloads' }))
    await screen.findByText('0 of 4 done')

    // Back and forward again: the job is shown, not started a second time.
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await heading('Add-ons')
    await next()
    await heading('Downloads')
    expect(await screen.findByText('0 of 4 done')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start downloads' })).toBeNull()

    const installs = calls.filter((c) => c.path === '/api/setup/install')
    expect(installs).toHaveLength(1)
    expect(bodyOf(installs[0])).toEqual({
      secret: SECRET,
      items: ['llama-cuda', 'chat-gemma-4-12b', 'comfyui', 'image-z-image-turbo'],
      make_default: true,
    })
  })

  it('a job that is already there (after a reload) is shown, not started again', async () => {
    window.sessionStorage.setItem('hk.setup.step', '4')
    const { calls } = wizardRoutes({
      'POST /api/setup/install/status': { body: { job: runningJob(['llama-cuda', 'chat-gemma-4-12b']) } },
    })
    renderWizard()
    expect(await screen.findByText('0 of 2 done')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start downloads' })).toBeNull()
    expect(calls.some((c) => c.path === '/api/setup/install')).toBe(false)
  })

  it('start at login is on by default and can be turned off', async () => {
    window.sessionStorage.setItem('hk.setup.step', '3')
    const { calls } = wizardRoutes()
    renderWizard()
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    const posted = calls.filter((c) => c.path === '/api/setup/startup')
    expect(posted.map(bodyOf)).toEqual([{ secret: SECRET, enabled: false }])
  })

  it('start at login is hidden on a PC that is not Windows', async () => {
    window.sessionStorage.setItem('hk.setup.step', '3')
    wizardRoutes({ 'POST /api/setup/addons': addonsReply({ startup: 'unsupported' }) })
    renderWizard()
    await screen.findByRole('heading', { level: 3, name: 'Phone and devices' })
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('analysis build and docker-missing states render', async () => {
    window.sessionStorage.setItem('hk.setup.step', '3')
    let docker = false
    const { calls } = wizardRoutes({
      'POST /api/setup/addons': () => addonsReply({ analysis: docker ? 'needs_image' : 'not_set_up' }),
      'POST /api/setup/sandbox/build': {
        status: 202,
        body: { build: { state: 'building', step: 'sandbox', reason: null } },
      },
    })
    renderWizard()
    expect(await screen.findByText('Install Docker Desktop and start it, then Check again.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Get Docker Desktop' })).toBeInTheDocument()
    docker = true
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Build the sandbox' }))
    expect(await screen.findByText('Building the sandbox, step 1 of 2.')).toBeInTheDocument()
    const asked = calls.filter((c) => c.path === '/api/setup/addons').map(bodyOf)
    expect(asked).toContainEqual({ secret: SECRET, refresh: true })
    expect(bodyOf(calls.find((c) => c.path === '/api/setup/sandbox/build')!)).toEqual({ secret: SECRET })
  })

  it('next is allowed while downloads run', async () => {
    window.sessionStorage.setItem('hk.setup.step', '4')
    wizardRoutes({
      'POST /api/setup/install/status': { body: { job: runningJob(['llama-cuda', 'chat-gemma-4-12b']) } },
    })
    renderWizard()
    expect(await screen.findByText(/They keep going while you set up your passkey\./)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop downloading' })).toBeInTheDocument()
    await next()
    expect(await heading('Your passkey')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Skip for now' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull()
  })

  it('phone card points to Settings later', async () => {
    window.sessionStorage.setItem('hk.setup.step', '3')
    wizardRoutes()
    renderWizard()
    const phone = (await screen.findByRole('heading', { level: 3, name: 'Phone and devices' })).closest('article')!
    expect(within(phone).getByText('You can add your phone later, from Settings.')).toBeInTheDocument()
    const github = screen.getByRole('heading', { level: 3, name: 'GitHub' }).closest('article')!
    expect(within(github).getByText('Add your GitHub token in Settings › GitHub after setup.')).toBeInTheDocument()
  })

  it('stepper marks the current step', async () => {
    wizardRoutes()
    renderWizard()
    const steps = await screen.findByRole('list', { name: 'Setup steps' })
    const current = () =>
      within(steps)
        .getAllByRole('listitem')
        .filter((li) => li.getAttribute('aria-current') === 'step')
    expect(current().map((li) => li.textContent)).toEqual([expect.stringContaining('Welcome')])
    expect(within(steps).getAllByRole('listitem')).toHaveLength(7)
    await next()
    const title = await heading('Your PC')
    expect(current().map((li) => li.textContent)).toEqual([expect.stringContaining('Your PC')])
    expect(title).toHaveFocus()
  })

  it('every string passes forbiddenIn', async () => {
    const seen: string[] = []
    wizardRoutes({
      ...installRoutes(runningJob(['llama-cuda', 'chat-gemma-4-12b'])),
      'POST /api/setup/addons': addonsReply({ analysis: 'not_set_up' }),
    })
    const { container } = renderWizard()
    await heading('Welcome to Hearthkeep')
    await screen.findByText(/free space/)
    seen.push(container.textContent ?? '')
    await next()
    await screen.findByText(HOME)
    fireEvent.click(screen.getByRole('button', { name: 'Change…' }))
    seen.push(container.textContent ?? '')
    await next()
    fireEvent.click(await screen.findByRole('button', { name: 'Choose Gemma 4 12B' }))
    seen.push(container.textContent ?? '')
    await next()
    await screen.findByText('You can add your phone later, from Settings.')
    seen.push(container.textContent ?? '')
    await next()
    await screen.findByRole('group', { name: 'Ready to download' })
    seen.push(container.textContent ?? '')
    fireEvent.click(screen.getByRole('button', { name: 'Start downloads' }))
    await screen.findByText(/They keep going/)
    seen.push(container.textContent ?? '')
    await next()
    fireEvent.click(await screen.findByRole('button', { name: 'Create passkey' }))
    await heading('Hearthkeep is ready')
    seen.push(container.textContent ?? '')
    for (const text of seen) expect(forbiddenIn(text)).toEqual([])
    for (const text of Object.values(SETUP_COPY)) expect(forbiddenIn(text)).toEqual([])
  })
})
