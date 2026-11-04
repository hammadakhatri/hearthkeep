import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { QrCode } from '../components/QrCode'
import { Settings } from '../views/Settings'
import { forbiddenIn } from '../test/copyRules'
import { bodyOf, mockFetch, modelsIdle, serverInfo, type Handler, type Reply } from '../test/fetch'
import { STARTUP_FAILED } from '../components/StartupSwitch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const MINUTE = 60_000
const laptop = {
  id: 'cred-1',
  device_name: 'Laptop',
  created_at: '2026-10-01T09:00:00Z',
  current: true,
  last_used_at: new Date(Date.now() - 10_000).toISOString(),
}
const phone = {
  id: 'cred-2',
  device_name: 'Phone',
  created_at: '2026-10-02T09:00:00Z',
  current: false,
  last_used_at: new Date(Date.now() - 5 * 60 * MINUTE).toISOString(),
}
const startupOff = { supported: true, enabled: false, other_data_folder: false }
const noFolders = { folders: [], suggestions: [] }

function routes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'GET /api/devices': { body: [laptop, phone] },
    'GET /api/folders': { body: noFolders },
    'GET /api/settings/startup': { body: startupOff },
    'GET /api/server': serverInfo(),
    ...modelsIdle,
    ...extra,
  })
}

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('step-token')
})

function startupSection(): HTMLElement {
  return screen.getByRole('heading', { level: 2, name: 'Start at login' }).closest('section')!
}

describe('Settings', () => {
  it('lists devices with this device and last used', async () => {
    routes()
    render(<Settings />)
    const devices = await screen.findByRole('list', { name: 'Devices' })
    const rows = within(devices).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Laptop')).toBeInTheDocument()
    expect(within(rows[0]).getByText('This device')).toBeInTheDocument()
    expect(within(rows[0]).getByText(/^Added /)).toBeInTheDocument()
    expect(within(rows[0]).getByText('Last used just now')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Last used 5 hours ago')).toBeInTheDocument()
    expect(within(rows[1]).queryByText('This device')).toBeNull()
    for (const name of ['Devices', 'Folders Hearthkeep can see', 'Notifications', 'Start at login', 'General']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(screen.getByText('Notifications on this device')).toBeInTheDocument()
  })

  it('add device asks for a passkey then shows the code and qr', async () => {
    const { calls } = routes({
      'POST /api/devices/add-code': { status: 201, body: { code: 'ABCD2345', expires_in_minutes: 15 } },
    })
    render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add a device' }))
    expect(await screen.findByText('ABCD2345')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('add_device')
    const add = calls.find((c) => c.path === '/api/devices/add-code')!
    expect(bodyOf(add)).toEqual({ step_up_token: 'step-token' })
    expect(screen.getByText('Valid for 15 minutes')).toBeInTheDocument()
    const qr = screen.getByRole('img', { name: 'QR code for the setup link' })
    expect(qr.tagName.toLowerCase()).toBe('svg')
    expect(screen.getByText(`${window.location.origin}/#enroll=ABCD2345`)).toBeInTheDocument()
  })

  it('rename device', async () => {
    const { calls } = routes({
      'PATCH /api/devices/cred-2': (init) => ({
        body: { ...phone, device_name: String(bodyOf({ path: '', init }).device_name).trim() },
      }),
    })
    render(<Settings />)
    const row = (await screen.findByText('Phone')).closest('li')!
    fireEvent.click(within(row).getByRole('button', { name: 'Rename Phone' }))
    const field = within(row).getByRole('textbox', { name: 'Device name' })
    expect(field).toHaveValue('Phone')
    fireEvent.change(field, { target: { value: 'Work phone ' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }))
    expect(await within(row).findByText('Work phone')).toBeInTheDocument()
    const patch = calls.find((c) => c.init.method === 'PATCH')!
    expect(bodyOf(patch)).toEqual({ device_name: 'Work phone' })
    expect(stepUp).not.toHaveBeenCalled()
  })

  it('cannot remove the only device', async () => {
    const { calls } = routes({ 'GET /api/devices': { body: [laptop] } })
    render(<Settings />)
    const row = (await screen.findByText('Laptop')).closest('li')!
    const remove = within(row).getByRole('button', { name: 'Remove Laptop' })
    expect(remove).toBeDisabled()
    expect(screen.getByText("You can't remove your only device. Add another first.")).toBeInTheDocument()
    fireEvent.click(remove)
    expect(calls.some((c) => c.init.method === 'DELETE')).toBe(false)
  })

  it('removes another device after confirming', async () => {
    let devices = [laptop, phone]
    const { calls } = routes({
      'GET /api/devices': () => ({ body: devices }),
      'DELETE /api/devices/cred-2': () => {
        devices = [laptop]
        return { body: { ok: true } }
      },
    })
    render(<Settings />)
    const row = (await screen.findByText('Phone')).closest('li')!
    fireEvent.click(within(row).getByRole('button', { name: 'Remove Phone' }))
    fireEvent.click(within(row).getByRole('button', { name: 'Yes, remove' }))
    await waitFor(() => expect(screen.queryByText('Phone')).toBeNull())
    expect(calls.filter((c) => c.init.method === 'DELETE')).toHaveLength(1)
  })

  it('sign out other sessions', async () => {
    routes({ 'POST /api/sessions/sign-out-others': { body: { signed_out: 2 } } })
    render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out other sessions' }))
    expect(await screen.findByText('Signed out 2 other sessions.')).toBeInTheDocument()
    expect(stepUp).not.toHaveBeenCalled()
  })

  it('adding a folder needs a passkey', async () => {
    const documents = { label: 'Documents', path: '<home>/Documents' }
    let listed = { folders: [] as { id: string; label: string; path: string }[], suggestions: [documents] }
    const { calls } = routes({
      'GET /api/folders': () => ({ body: listed }),
      'POST /api/folders': (init) => {
        const path = String(bodyOf({ path: '', init }).path)
        const folder = { id: 'f1', label: 'Documents', path }
        listed = { folders: [folder], suggestions: [] }
        return { status: 201, body: folder }
      },
      'DELETE /api/folders/f1': () => {
        listed = { folders: [], suggestions: [documents] }
        return { body: { ok: true } }
      },
    })
    render(<Settings />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add Documents' }))
    const list = await screen.findByRole('list', { name: 'Folders Hearthkeep can see' })
    expect(await within(list).findByText(documents.path)).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('add_folder')
    const add = calls.find((c) => c.path === '/api/folders' && c.init.method === 'POST')!
    expect(bodyOf(add)).toEqual({ path: documents.path, step_up_token: 'step-token' })

    // A typed path, refused by the server, says why.
    const refused = mockFetch({
      'GET /api/devices': { body: [laptop, phone] },
      'GET /api/folders': () => ({ body: listed }),
      'GET /api/settings/startup': { body: startupOff },
      'POST /api/folders': { status: 422, body: { reason: 'folder_refused', message: "That folder doesn't exist." } },
      'DELETE /api/folders/f1': () => {
        listed = { folders: [], suggestions: [documents] }
        return { body: { ok: true } }
      },
    })
    fireEvent.change(screen.getByRole('textbox', { name: 'Or type a folder path on this PC' }), {
      target: { value: '<nowhere>' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))
    expect(await screen.findByText("That folder doesn't exist.")).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledTimes(2)

    // Removing needs only the session.
    fireEvent.click(within(list).getByRole('button', { name: 'Remove Documents' }))
    await waitFor(() => expect(within(list).queryByText(documents.path)).toBeNull())
    expect(stepUp).toHaveBeenCalledTimes(2)
    expect(refused.calls.some((c) => c.init.method === 'DELETE')).toBe(true)
  })

  it('start at login is a labelled switch, and no command text is shown', async () => {
    routes()
    render(<Settings />)
    const section = (await screen.findByRole('heading', { level: 2, name: 'Start at login' })).closest('section')!
    const toggle = await within(section).findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(within(section).getByText('Off')).toBeInTheDocument()
    expect(within(section).queryByRole('button', { name: 'Copy command' })).toBeNull()
    expect(within(section).queryByText(/run this on the PC/)).toBeNull()
    expect(forbiddenIn(section.textContent ?? '')).toEqual([])
    expect(section.querySelector('code')).toBeNull()
  })

  it('settings startup switch turns on with a passkey and off without', async () => {
    let enabled = false
    const { calls } = routes({
      'GET /api/settings/startup': () => ({ body: { ...startupOff, enabled } }),
      'POST /api/settings/startup': (init) => {
        enabled = JSON.parse(String(init.body)).enabled
        return { body: { ...startupOff, enabled } }
      },
    })
    render(<Settings />)
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    await waitFor(() => expect(toggle).toBeEnabled())

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('settings')
    const on = calls.filter((c) => c.init.method === 'POST' && c.path === '/api/settings/startup')
    expect(on.map(bodyOf)).toEqual([{ enabled: true, step_up_token: 'step-token' }])
    expect(screen.getByText('On')).toBeInTheDocument()

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    expect(stepUp).toHaveBeenCalledTimes(1) // turning it off needs only the session
    const posts = calls.filter((c) => c.init.method === 'POST' && c.path === '/api/settings/startup')
    expect(posts.map(bodyOf)).toEqual([{ enabled: true, step_up_token: 'step-token' }, { enabled: false }])
  })

  it('a cancelled passkey leaves the switch off and says so', async () => {
    vi.mocked(stepUp).mockRejectedValue(Object.assign(new Error('x'), { name: 'NotAllowedError' }))
    const { calls } = routes()
    render(<Settings />)
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    expect(await within(startupSection()).findByRole('alert')).toHaveTextContent('was cancelled')
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(calls.some((c) => c.init.method === 'POST' && c.path === '/api/settings/startup')).toBe(false)
  })

  it('a failed change shows the server message and keeps the old state', async () => {
    const message = "Start at login couldn't be changed. Try again."
    routes({ 'POST /api/settings/startup': { status: 502, body: { reason: 'startup_failed', message } } })
    render(<Settings />)
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    const alert = await within(startupSection()).findByRole('alert')
    expect(alert).toHaveTextContent(message)
    expect(forbiddenIn(alert.textContent ?? '')).toEqual([])
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(toggle).toBeEnabled()
  })

  it("a server sentence on any other error isn't shown: the plain one is", async () => {
    routes({
      'POST /api/settings/startup': { status: 409, body: { reason: 'busy', message: 'Something internal happened.' } },
    })
    render(<Settings />)
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    const alert = await within(startupSection()).findByRole('alert')
    expect(alert).toHaveTextContent(STARTUP_FAILED)
    expect(alert).not.toHaveTextContent('Something internal happened.')
  })

  it('a startup_failed answer with no sentence uses the shared one', async () => {
    routes({ 'POST /api/settings/startup': { status: 502, body: { reason: 'startup_failed' } } })
    render(<Settings />)
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    expect(await within(startupSection()).findByRole('alert')).toHaveTextContent(STARTUP_FAILED)
  })

  it('start at login says when it is on for another data folder', async () => {
    const elsewhere = { supported: true, enabled: true, other_data_folder: true }
    routes({ 'GET /api/settings/startup': { body: elsewhere } })
    render(<Settings />)
    expect(await screen.findByText('On, for another data folder')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
    const line = screen.getByText('Switching this on makes it start this Hearthkeep instead.')
    expect(forbiddenIn(line.textContent ?? '')).toEqual([])
  })

  it('the switch toggles from the keyboard and keeps focus while busy', async () => {
    let release: (r: { body: unknown }) => void = () => {}
    const { calls } = routes({
      'POST /api/settings/startup': () => new Promise((resolve) => (release = resolve)),
    })
    render(<Settings />)
    const toggle = await screen.findByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })
    await waitFor(() => expect(toggle).toBeEnabled())
    // A native button: the browser turns Space and Enter into this click (jsdom does not).
    expect(toggle.tagName).toBe('BUTTON')
    toggle.focus()
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-disabled', 'true'))
    expect(toggle).toHaveFocus()
    fireEvent.click(toggle) // ignored while busy
    expect(calls.filter((c) => c.init.method === 'POST' && c.path === '/api/settings/startup')).toHaveLength(1)
    release({ body: { supported: true, enabled: true, other_data_folder: false } })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    expect(toggle).toHaveFocus()
  })

  it('start at login says only on Windows elsewhere', async () => {
    routes({ 'GET /api/settings/startup': { body: { supported: false, enabled: false } } })
    render(<Settings />)
    expect(await screen.findByText('Only on Windows')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Start Hearthkeep when I sign in to this PC' })).toBeDisabled()
  })
})

describe('QrCode', () => {
  it('qr renders as svg without innerHTML', () => {
    const spy = vi.spyOn(Element.prototype, 'innerHTML', 'set')
    const { container } = render(<QrCode text="https://hearthkeep.example/#enroll=ABCD2345" size={160} />)
    const svg = screen.getByRole('img', { name: 'QR code for the setup link' })
    expect(svg.tagName.toLowerCase()).toBe('svg')
    expect(svg).toHaveAttribute('width', '160')
    const rects = container.querySelectorAll('svg rect')
    expect(rects.length).toBeGreaterThan(100)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
