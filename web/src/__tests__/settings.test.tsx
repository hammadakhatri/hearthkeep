import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { QrCode } from '../components/QrCode'
import { Settings } from '../views/Settings'
import { bodyOf, mockFetch, type Handler, type Reply } from '../test/fetch'

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
const startupOff = {
  supported: true,
  enabled: false,
  command: 'hearthkeep startup enable --data "<data>"',
}
const noFolders = { folders: [], suggestions: [] }

function routes(extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'GET /api/devices': { body: [laptop, phone] },
    'GET /api/folders': { body: noFolders },
    'GET /api/settings/startup': { body: startupOff },
    ...extra,
  })
}

beforeEach(() => {
  vi.mocked(stepUp).mockReset().mockResolvedValue('step-token')
})

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
    for (const name of ['Devices', 'Folders Hearthkeep can see', 'Notifications', 'Start at login']) {
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

  it('start at login shows status and command', async () => {
    routes()
    render(<Settings />)
    const section = (await screen.findByRole('heading', { level: 2, name: 'Start at login' })).closest('section')!
    expect(await within(section).findByText('Off')).toBeInTheDocument()
    expect(within(section).getByText('To change it, run this on the PC:')).toBeInTheDocument()
    expect(within(section).getByText(startupOff.command)).toBeInTheDocument()
    expect(within(section).getByRole('button', { name: 'Copy command' })).toBeInTheDocument()
  })

  it('start at login says only on Windows elsewhere', async () => {
    routes({ 'GET /api/settings/startup': { body: { supported: false, enabled: false, command: null } } })
    render(<Settings />)
    expect(await screen.findByText('Only on Windows')).toBeInTheDocument()
    expect(screen.queryByText('To change it, run this on the PC:')).toBeNull()
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
