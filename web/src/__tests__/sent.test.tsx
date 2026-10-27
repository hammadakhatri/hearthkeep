import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SentItemInfo } from '../api/sent'
import { FindingsCard } from '../components/FindingsCard'
import { SendMenu } from '../components/SendMenu'
import { SentTray } from '../components/SentTray'
import { AppShell } from '../shell/AppShell'
import { applyEvent, itemsFromMessages, liveTurn, type AssistantItem, type ServerMessage } from '../views/chatModel'
import { bodyOf, healthy, mockFetch } from '../test/fetch'

const embed = vi.hoisted(() => vi.fn())
vi.mock('vega-embed', () => ({ default: embed }))

const PNG_URL = 'data:image/png;base64,iVBORw=='

const DEVICES = [
  { id: 'cred-1', device_name: 'Laptop', created_at: '2026-10-26T00:00:00Z', current: true },
  { id: 'cred-2', device_name: '<b>Phone</b>', created_at: '2026-10-26T00:00:00Z', current: false },
]

const NO_PUSH = { status: 503, body: { reason: 'push_unavailable' } }

function sentItem(extra: Partial<SentItemInfo> = {}): SentItemInfo {
  return {
    id: 's1',
    kind: 'table',
    title: 'Negative amounts',
    text: null,
    marks: null,
    chart: null,
    created_at: '2026-10-27T10:00:00Z',
    dismissed: false,
    ...extra,
  }
}

beforeEach(() => {
  embed.mockReset()
  embed.mockImplementation(async (el: HTMLElement) => {
    el.innerHTML = '<svg data-chart="yes"></svg>'
    return { view: { toImageURL: vi.fn(async () => PNG_URL) }, finalize: vi.fn() }
  })
})

afterEach(() => {
  Reflect.deleteProperty(navigator, 'serviceWorker')
  window.location.hash = ''
  vi.useRealTimers()
})

describe('send menu', () => {
  it('send menu sends a chart to all devices', async () => {
    const { calls } = mockFetch({
      'GET /api/devices': { body: DEVICES },
      'POST /api/sent': { status: 201, body: { id: 's1' } },
    })
    render(<SendMenu kind="chart" refId="o1" label="Sales by week" />)
    fireEvent.click(screen.getByRole('button', { name: 'Send Sales by week' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Send to all my devices' }))
    expect(await screen.findByText('Sent.')).toBeInTheDocument()
    const post = calls.find((c) => c.path === '/api/sent')!
    expect(bodyOf(post)).toEqual({ kind: 'chart', ref_id: 'o1', devices: 'all' })
  })

  it('send menu lists devices by name', async () => {
    const { calls } = mockFetch({
      'GET /api/devices': { body: DEVICES },
      'POST /api/sent': { status: 201, body: { id: 's1' } },
    })
    render(<SendMenu kind="table" refId="o2" label="Negative amounts" />)
    const button = screen.getByRole('button', { name: 'Send Negative amounts' })
    expect(button).toHaveAttribute('aria-haspopup', 'menu')
    fireEvent.click(button)
    await screen.findByRole('menuitem', { name: '<b>Phone</b>' })
    const items = screen.getAllByRole('menuitem')
    // Names are shown as text, never as markup.
    expect(items.map((i) => i.textContent)).toEqual(['Send to all my devices', 'Laptop', '<b>Phone</b>'])
    fireEvent.click(items[2])
    await screen.findByText('Sent.')
    expect(bodyOf(calls.find((c) => c.path === '/api/sent')!)).toEqual({
      kind: 'table',
      ref_id: 'o2',
      devices: ['cred-2'],
    })
  })

  it('moves through the menu with the arrow keys and closes on Escape', async () => {
    mockFetch({ 'GET /api/devices': { body: DEVICES } })
    render(<SendMenu kind="file" refId="o3" label="cleaned.csv" />)
    const button = screen.getByRole('button', { name: 'Send cleaned.csv' })
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    await screen.findByRole('menuitem', { name: '<b>Phone</b>' })
    const items = screen.getAllByRole('menuitem')
    await waitFor(() => expect(items[0]).toHaveFocus())
    fireEvent.keyDown(items[0], { key: 'ArrowDown' })
    expect(items[1]).toHaveFocus()
    fireEvent.keyDown(items[1], { key: 'ArrowUp' })
    fireEvent.keyDown(items[0], { key: 'ArrowUp' })
    expect(items[2]).toHaveFocus()
    fireEvent.keyDown(items[2], { key: 'Home' })
    expect(items[0]).toHaveFocus()
    fireEvent.keyDown(items[0], { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(button).toHaveFocus()
  })

  it('is disabled while a send is in flight and shows a failure', async () => {
    let finish: (r: { status: number; body: unknown }) => void = () => {}
    mockFetch({
      'GET /api/devices': { body: [] },
      'POST /api/sent': () => new Promise((resolve) => (finish = resolve)),
    })
    render(<SendMenu kind="chart" refId="o1" label="Sales" />)
    const button = screen.getByRole('button', { name: 'Send Sales' })
    fireEvent.click(button)
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Send to all my devices' }))
    await waitFor(() => expect(button).toBeDisabled())
    await act(async () => finish({ status: 404, body: { reason: 'not_found' } }))
    await waitFor(() => expect(button).toBeEnabled())
    expect(screen.getByRole('alert')).toHaveTextContent(/can't be sent/i)
  })

  it('summary send passes the message id not the text', async () => {
    const { calls } = mockFetch({
      'GET /api/devices': { body: [] },
      'POST /api/sent': { status: 201, body: { id: 's1' } },
    })
    const item: AssistantItem = {
      ...liveTurn('t1', 0),
      state: 'done',
      text: 'You spent 4,210 in March.',
      messageId: 42,
      steps: [{ id: 's1', tool: 'python', summary: 'Run Python', state: 'ok', runs: ['r1'] }],
    }
    render(<FindingsCard item={item} runs={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'Send summary' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Send to all my devices' }))
    await screen.findByText('Sent.')
    const body = bodyOf(calls.find((c) => c.path === '/api/sent')!)
    expect(body).toEqual({ kind: 'summary', ref_id: '42', devices: 'all' })
    expect(JSON.stringify(body)).not.toContain('4,210')
  })

  it('says so, quietly, when the device list cannot be loaded', async () => {
    mockFetch({ 'GET /api/devices': { status: 500, body: { reason: 'boom' } } })
    render(<SendMenu kind="chart" refId="o1" label="Sales" />)
    fireEvent.click(screen.getByRole('button', { name: 'Send Sales' }))
    expect(
      await screen.findByText("Couldn't load your devices; you can still send to all of them."),
    ).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Send to all my devices' })).toBeInTheDocument()
  })

  it('a summary that is too long says to send the chart or rows instead', async () => {
    mockFetch({
      'GET /api/devices': { body: [] },
      'POST /api/sent': { status: 422, body: { reason: 'too_long' } },
    })
    render(<SendMenu kind="summary" refId="42" label="summary" />)
    fireEvent.click(screen.getByRole('button', { name: 'Send summary' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Send to all my devices' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too long to send as a summary. Send the chart or rows instead.',
    )
  })
})

describe('summary message id', () => {
  const at = '2026-10-27T10:00:00Z'
  const base = { tool_calls: null, tool_call_id: null, created_at: at }

  it('a multi-message turn sends the id whose stored text equals the card text', async () => {
    const joined = 'Let me count.\n\nThere are 5 rows.'
    const messages: ServerMessage[] = [
      { ...base, id: 1, role: 'user', content: 'How many?' },
      { ...base, id: 2, role: 'assistant', content: 'Let me count.', tool_calls: [{ id: 'c1', function: { name: 'python' } }] },
      { ...base, id: 3, role: 'tool', content: 'ok', tool_call_id: 'c1' },
      { ...base, id: 4, role: 'assistant', content: 'There are 5 rows.', number_check: { text: joined, marks: [], message_id: 4 } },
    ]
    const item = itemsFromMessages(messages)[1] as AssistantItem
    expect(item.text).toBe(item.check?.text)
    expect(item.messageId).toBe(4)
    const { calls } = mockFetch({
      'GET /api/devices': { body: [] },
      'POST /api/sent': { status: 201, body: { id: 's1' } },
    })
    render(<FindingsCard item={item} runs={[]} />)
    fireEvent.click(screen.getByRole('button', { name: 'Send summary' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Send to all my devices' }))
    await screen.findByText('Sent.')
    expect(bodyOf(calls.find((c) => c.path === '/api/sent')!).ref_id).toBe('4')
  })

  it('a live turn takes the id from the number check, else from turn_done', () => {
    let item = { ...liveTurn('t1', 0) }
    item = applyEvent(item, { type: 'text_delta', seq: 1, text: 'Hi' }, 0)
    expect(applyEvent(item, { type: 'turn_done', seq: 2, message_id: 7 }, 0).messageId).toBe(7)
    const checked = applyEvent(item, { type: 'number_check', seq: 2, text: 'Hi', marks: [], message_id: 9 }, 0)
    expect(checked.messageId).toBe(9)
    expect(applyEvent(checked, { type: 'turn_done', seq: 3, message_id: 7 }, 0).messageId).toBe(9)
  })

  it('a saved turn names its final reply for sending', () => {
    const items = itemsFromMessages([
      { ...base, id: 1, role: 'user', content: 'How much?' },
      { ...base, id: 2, role: 'assistant', content: 'Let me check.' },
      { ...base, id: 3, role: 'assistant', content: 'You spent 4,210.' },
    ])
    expect(items[1]).toMatchObject({ kind: 'assistant', messageId: 3 })
  })
})

describe('sent tray', () => {
  it('tray shows items and dismiss hides them', async () => {
    const { calls } = mockFetch({
      'GET /api/sent': {
        body: {
          items: [
            sentItem({ id: 's1', title: 'Negative amounts' }),
            sentItem({ id: 's2', kind: 'file', title: 'cleaned.csv' }),
            sentItem({ id: 's3', title: 'Old rows', dismissed: true }),
          ],
          unread: 2,
        },
      },
      'POST /api/sent/s1/dismiss': { body: { dismissed: true } },
      'GET /api/sent/count': { body: { unread: 1 } },
      'GET /api/push': NO_PUSH,
    })
    render(<SentTray open onClose={vi.fn()} onUnread={vi.fn()} />)
    const dialog = await screen.findByRole('dialog', { name: 'Sent to you' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(await within(dialog).findByText('Negative amounts')).toBeInTheDocument()
    expect(within(dialog).queryByText('Old rows')).toBeNull()
    const downloads = within(dialog).getAllByRole('link', { name: /Download/ })
    expect(downloads.map((a) => a.getAttribute('href'))).toEqual(['/api/sent/s1/download', '/api/sent/s2/download'])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Dismiss Negative amounts' }))
    expect(within(dialog).queryByText('Negative amounts')).toBeNull()
    await waitFor(() => expect(calls.some((c) => c.path === '/api/sent/s1/dismiss')).toBe(true))
    expect(within(dialog).getByText('cleaned.csv')).toBeInTheDocument()
  })

  it('closes on a click that started on the scrim, not one that started inside', async () => {
    mockFetch({ 'GET /api/sent': { body: { items: [], unread: 0 } }, 'GET /api/push': NO_PUSH })
    const onClose = vi.fn()
    const { container } = render(<SentTray open onClose={onClose} onUnread={vi.fn()} />)
    const dialog = await screen.findByRole('dialog')
    const scrim = container.querySelector('.sent-scrim') as HTMLElement
    fireEvent.pointerDown(dialog)
    fireEvent.click(scrim)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.pointerDown(scrim)
    fireEvent.click(scrim)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('puts a dismissed item back and says so when the dismiss fails', async () => {
    mockFetch({
      'GET /api/sent': { body: { items: [sentItem()], unread: 1 } },
      'POST /api/sent/s1/dismiss': { status: 500, body: { reason: 'boom' } },
      'GET /api/push': NO_PUSH,
    })
    render(<SentTray open onClose={vi.fn()} onUnread={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss Negative amounts' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't dismiss/i)
    expect(screen.getByText('Negative amounts')).toBeInTheDocument()
  })

  it('says what is here when nothing was sent, and has the notifications switch', async () => {
    mockFetch({ 'GET /api/sent': { body: { items: [], unread: 0 } }, 'GET /api/push': NO_PUSH })
    render(<SentTray open onClose={vi.fn()} onUnread={vi.fn()} />)
    expect(
      await screen.findByText('Nothing here yet. Charts, tables and files sent to this device appear here.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Notifications on this device')).toBeInTheDocument()
  })

  it('chart item renders and saves as png', async () => {
    mockFetch({
      'GET /api/sent': {
        body: {
          items: [
            sentItem({ kind: 'chart', title: 'Sales by week', chart: { mark: 'line', data: { values: [{ a: 1 }] } } }),
          ],
          unread: 1,
        },
      },
      'GET /api/push': NO_PUSH,
    })
    const created = vi.fn((_: Blob) => 'blob:chart')
    // URL is shared by every test: put its two methods back afterwards.
    const originals = { create: URL.createObjectURL, revoke: URL.revokeObjectURL }
    URL.createObjectURL = created
    URL.revokeObjectURL = vi.fn()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<SentTray open onClose={vi.fn()} onUnread={vi.fn()} />)
    const dialog = await screen.findByRole('dialog', { name: 'Sent to you' })
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1))
    expect(embed.mock.calls[0][2]).toMatchObject({ mode: 'vega-lite', actions: false, ast: true })
    const save = within(dialog).getByRole('button', { name: 'Save as PNG' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(created.mock.calls[0][0].type).toBe('image/png')
    // A chart is saved on the device: no download link.
    expect(within(dialog).queryByRole('link', { name: /Download/ })).toBeNull()
    click.mockRestore()
    URL.createObjectURL = originals.create
    URL.revokeObjectURL = originals.revoke
  })

  it('summary item shows number marks', async () => {
    const text = 'You spent 4,210 in March, about 31%.'
    const at = text.indexOf('4,210')
    const pct = text.indexOf('31%')
    mockFetch({
      'GET /api/sent': {
        body: {
          items: [
            sentItem({
              kind: 'summary',
              title: 'Summary',
              text,
              marks: [
                { start: at, end: at + 5, text: '4,210', kind: 'computed', page: null, note: null },
                { start: pct, end: pct + 3, text: '31%', kind: 'unverified', page: null, note: 'Not found in any result' },
              ],
            }),
          ],
          unread: 1,
        },
      },
      'GET /api/push': NO_PUSH,
    })
    render(<SentTray open onClose={vi.fn()} onUnread={vi.fn()} />)
    const computed = await screen.findByText('4,210')
    expect(computed).toHaveClass('num-computed')
    const unverified = screen.getByRole('button', { name: '31%' })
    expect(unverified).toHaveClass('num-unverified')
    fireEvent.click(unverified)
    expect(screen.getByText('Unverified: Not found in any result')).toBeVisible()
    expect(screen.getByRole('link', { name: /Download/ })).toHaveAttribute('href', '/api/sent/s1/download')
  })

  it('traps focus, closes on Escape and returns focus to the opener', async () => {
    mockFetch({ 'GET /api/sent': { body: { items: [sentItem()], unread: 1 } }, 'GET /api/push': NO_PUSH })
    function Harness() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button onClick={() => setOpen(true)}>opener</button>
          <SentTray open={open} onClose={() => setOpen(false)} onUnread={vi.fn()} />
        </>
      )
    }
    render(<Harness />)
    const opener = screen.getByRole('button', { name: 'opener' })
    opener.focus()
    fireEvent.click(opener)
    const dialog = await screen.findByRole('dialog', { name: 'Sent to you' })
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement))
    // Shift+Tab from the first control wraps to the last, and Tab from the last wraps to the first.
    const close = within(dialog).getByRole('button', { name: 'Close' })
    const dismiss = await within(dialog).findByRole('button', { name: 'Dismiss Negative amounts' })
    close.focus()
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(dismiss).toHaveFocus()
    fireEvent.keyDown(dismiss, { key: 'Tab' })
    expect(close).toHaveFocus()
    // Focus that has strayed outside comes back in.
    ;(document.activeElement as HTMLElement).blur()
    fireEvent.keyDown(document.body, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(opener).toHaveFocus()
  })
})

describe('top bar badge and opening the tray', () => {
  const baseRoutes = { 'GET /api/health': healthy, 'GET /api/push': NO_PUSH }

  it('badge shows the unread count and caps at 9+', async () => {
    let unread = 3
    mockFetch({ ...baseRoutes, 'GET /api/sent/count': () => ({ body: { unread } }) })
    render(<AppShell onSignOut={() => {}} />)
    const button = await screen.findByRole('button', { name: 'Sent to you, 3 unread' })
    expect(within(button).getByText('3')).toBeInTheDocument()
    unread = 12
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    const capped = await screen.findByRole('button', { name: 'Sent to you, 12 unread' })
    expect(within(capped).getByText('9+')).toBeInTheDocument()
    unread = 0
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    const none = await screen.findByRole('button', { name: 'Sent to you' })
    expect(none.querySelector('.sent-badge')).toBeNull()
  })

  it('ignores a count that arrives after a newer one', async () => {
    const waiting: ((n: number) => void)[] = []
    mockFetch({
      ...baseRoutes,
      'GET /api/sent/count': () =>
        new Promise((resolve) => waiting.push((n) => resolve({ status: 200, body: { unread: n } }))),
    })
    render(<AppShell onSignOut={() => {}} />)
    await waitFor(() => expect(waiting).toHaveLength(1))
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await waitFor(() => expect(waiting).toHaveLength(2))
    await act(async () => waiting[1](5))
    await screen.findByRole('button', { name: 'Sent to you, 5 unread' })
    await act(async () => waiting[0](1))
    expect(screen.getByRole('button', { name: 'Sent to you, 5 unread' })).toBeInTheDocument()
  })

  it('makes the page behind the tray inert while it is open', async () => {
    mockFetch({
      ...baseRoutes,
      'GET /api/sent/count': { body: { unread: 0 } },
      'GET /api/sent': { body: { items: [], unread: 0 } },
    })
    const { container } = render(<AppShell onSignOut={() => {}} />)
    const shell = container.querySelector('.shell') as HTMLElement
    expect(shell).not.toHaveAttribute('inert')
    fireEvent.click(await screen.findByRole('button', { name: 'Sent to you' }))
    await screen.findByRole('dialog')
    expect(shell).toHaveAttribute('inert')
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(shell).not.toHaveAttribute('inert'))
  })

  it('refreshes every 30 seconds, only while the page is visible', async () => {
    vi.useFakeTimers()
    const { calls } = mockFetch({ ...baseRoutes, 'GET /api/sent/count': { body: { unread: 1 } } })
    render(<AppShell onSignOut={() => {}} />)
    const counts = () => calls.filter((c) => c.path === '/api/sent/count').length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    const first = counts()
    expect(first).toBeGreaterThanOrEqual(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(counts()).toBe(first + 1)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(counts()).toBe(first + 1)
    visibility.mockReturnValue('visible')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(counts()).toBe(first + 2)
    visibility.mockRestore()
  })

  it('opening #sent opens the tray', async () => {
    mockFetch({
      ...baseRoutes,
      'GET /api/sent/count': { body: { unread: 0 } },
      'GET /api/sent': { body: { items: [], unread: 0 } },
    })
    window.location.hash = '#sent'
    render(<AppShell onSignOut={() => {}} />)
    expect(await screen.findByRole('dialog', { name: 'Sent to you' })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })

  it('opens the tray when the service worker says open-sent, from this origin only', async () => {
    const worker = new EventTarget()
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: worker })
    mockFetch({
      ...baseRoutes,
      'GET /api/sent/count': { body: { unread: 0 } },
      'GET /api/sent': { body: { items: [], unread: 0 } },
    })
    render(<AppShell onSignOut={() => {}} />)
    act(() => {
      worker.dispatchEvent(
        new MessageEvent('message', { data: { type: 'open-sent' }, origin: 'https://elsewhere.example' }),
      )
    })
    expect(screen.queryByRole('dialog')).toBeNull()
    act(() => {
      worker.dispatchEvent(
        new MessageEvent('message', { data: { type: 'open-sent' }, origin: window.location.origin }),
      )
    })
    expect(await screen.findByRole('dialog', { name: 'Sent to you' })).toBeInTheDocument()
    Reflect.deleteProperty(navigator, 'serviceWorker')
  })

  it('accepts open-sent with no origin when the source is a same-origin service worker', async () => {
    const worker = new EventTarget()
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: worker })
    class FakeServiceWorker {
      scriptURL: string
      constructor(scriptURL: string) {
        this.scriptURL = scriptURL
      }
    }
    vi.stubGlobal('ServiceWorker', FakeServiceWorker)
    mockFetch({
      ...baseRoutes,
      'GET /api/sent/count': { body: { unread: 0 } },
      'GET /api/sent': { body: { items: [], unread: 0 } },
    })
    render(<AppShell onSignOut={() => {}} />)
    const send = (scriptURL: string) => {
      const event = new MessageEvent('message', { data: { type: 'open-sent' } })
      Object.defineProperty(event, 'source', { value: new FakeServiceWorker(scriptURL) })
      act(() => {
        worker.dispatchEvent(event)
      })
    }
    send('https://elsewhere.example/sw.js')
    expect(screen.queryByRole('dialog')).toBeNull()
    send(`${window.location.origin}/sw.js`)
    expect(await screen.findByRole('dialog', { name: 'Sent to you' })).toBeInTheDocument()
  })

  it('the top bar button opens the tray', async () => {
    mockFetch({
      ...baseRoutes,
      'GET /api/sent/count': { body: { unread: 0 } },
      'GET /api/sent': { body: { items: [], unread: 0 } },
    })
    render(<AppShell onSignOut={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sent to you' }))
    expect(await screen.findByRole('dialog', { name: 'Sent to you' })).toBeInTheDocument()
  })
})
