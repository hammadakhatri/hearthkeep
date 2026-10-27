import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { AppShell } from '../shell/AppShell'
import { Analysis } from '../views/Analysis'
import { bodyOf, healthy, mockFetch, type Reply } from '../test/fetch'
import { installEventSource } from '../test/eventsource'

const embed = vi.hoisted(() => vi.fn())
vi.mock('vega-embed', () => ({ default: embed }))
vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const CHAT_A = 'a'.repeat(32)
const CHAT_B = 'b'.repeat(32)
const EMPTY = 'Attach a file to a chat to start. Your files and charts collect here.'
const SPEC = { mark: 'bar', data: { values: [{ m: 'Jan', v: 2 }] }, encoding: {} }
const HEADLINE = 'Sales rose 31% to 4,210 in March.'

function file(id: string, chat: string, name: string) {
  return { id, chat_id: chat, name, kind: 'csv', size: 2048, status: 'ready', created_at: '2026-10-26T10:00:00Z' }
}

const CHAT_ROWS = [
  {
    chat_id: CHAT_A,
    title: 'Shop sales',
    updated_at: '2026-10-27T10:00:00.000000Z',
    files: [file('f1', CHAT_A, 'sales.csv'), file('f2', CHAT_A, 'costs.csv')],
    charts: [
      { id: 'o1', run_id: 'r1', title: 'Sales by week', created_at: '2026-10-27T10:00:01Z' },
      { id: 'o2', run_id: 'r1', title: 'Costs by shop', created_at: '2026-10-27T10:00:02Z' },
    ],
  },
  {
    chat_id: CHAT_B,
    title: 'Bank statement',
    updated_at: '2026-10-26T10:00:00.000000Z',
    files: [file('f3', CHAT_B, 'bank.pdf')],
    charts: [],
  },
]

function output(id: string, name: string, headline: unknown = null) {
  return { id, run_id: 'r1', chat_id: CHAT_A, kind: 'chart', name, spec: SPEC, turn_id: 't1', headline }
}

const MARK = { start: 22, end: 25, text: '31%', kind: 'unverified', page: null, note: 'matches nothing' }

function routes(extra: Record<string, Reply | ((init: RequestInit) => Reply | Promise<Reply>)> = {}) {
  return mockFetch({
    'GET /api/analysis?limit=20': { body: { chats: CHAT_ROWS } },
    'GET /api/health': healthy,
    'GET /api/outputs/o1': { body: output('o1', 'Sales by week', { text: HEADLINE, marks: [{ ...MARK, start: 11, end: 14 }] }) },
    'GET /api/outputs/o2': { body: output('o2', 'Costs by shop') },
    ...extra,
  })
}

beforeEach(() => {
  installEventSource()
  embed.mockReset()
  embed.mockImplementation(async (el: HTMLElement) => {
    el.innerHTML = '<svg data-chart="yes"></svg>'
    return { view: { toImageURL: vi.fn() }, finalize: vi.fn() }
  })
  vi.mocked(stepUp).mockReset().mockResolvedValue('delete-token')
})

describe('Analysis', () => {
  it('groups files and charts by chat', async () => {
    routes()
    render(<Analysis onOpenChat={() => {}} />)
    expect(screen.getByRole('heading', { name: 'Analysis' })).toBeInTheDocument()
    const shop = await screen.findByRole('region', { name: 'Shop sales' })
    expect(within(shop).getByText('sales.csv')).toBeInTheDocument()
    expect(within(shop).getByText('costs.csv')).toBeInTheDocument()
    expect(within(shop).getByRole('link', { name: 'Download sales.csv' })).toHaveAttribute('href', '/api/files/f1/download')
    expect(within(shop).getByRole('button', { name: 'Open Sales by week full size' })).toBeInTheDocument()
    expect(within(shop).getByRole('button', { name: 'Open Costs by shop full size' })).toBeInTheDocument()

    const bank = screen.getByRole('region', { name: 'Bank statement' })
    expect(within(bank).getByText('bank.pdf')).toBeInTheDocument()
    expect(within(bank).queryByRole('button', { name: /full size/ })).toBeNull()

    // Thumbnails draw the chart without tooltips or zoom.
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(2))
    expect(shop.querySelectorAll('.chart-canvas[data-static]')).toHaveLength(2)
    expect(embed.mock.calls[0][2].tooltip).toBe(false)
  })

  it('shows the empty text, and Load more asks for the next page', async () => {
    const { calls } = routes({ 'GET /api/analysis?limit=20': { body: { chats: [] } } })
    render(<Analysis onOpenChat={() => {}} />)
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(calls[0].path).toBe('/api/analysis?limit=20')
  })

  it('opens a chart full size with its headline', async () => {
    routes()
    render(<Analysis onOpenChat={() => {}} />)
    const opener = await screen.findByRole('button', { name: 'Open Sales by week full size' })
    opener.focus()
    fireEvent.click(opener)

    const dialog = await screen.findByRole('dialog', { name: 'Sales by week' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // The interactive chart, and the headline with a tappable note on its unverified number.
    await waitFor(() => expect(within(dialog).queryByRole('img', { name: 'Chart: Sales by week' })).toBeInTheDocument())
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(3))
    expect(dialog.querySelector('.chart-canvas:not([data-static])')).not.toBeNull()
    expect(dialog).toHaveTextContent('Sales rose 31%')
    expect(dialog).toHaveTextContent('to 4,210 in March.')
    const number = within(dialog).getByRole('button', { name: '31%' })
    expect(number).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(number)
    expect(number).toHaveAttribute('aria-expanded', 'true')
    expect(within(dialog).getByText('Unverified: matches nothing')).toBeVisible()

    // Focus is inside and kept there; Escape closes and gives it back.
    expect(dialog.contains(document.activeElement)).toBe(true)
    const buttons = [...dialog.querySelectorAll<HTMLElement>('button, a[href]')]
    buttons[buttons.length - 1].focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(dialog.contains(document.activeElement)).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })

  it('a chart with no saved headline still opens', async () => {
    routes()
    render(<Analysis onOpenChat={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open Costs by shop full size' }))
    const dialog = await screen.findByRole('dialog', { name: 'Costs by shop' })
    expect(within(dialog).getByRole('button', { name: 'Open in chat' })).toBeInTheDocument()
    expect(dialog.querySelector('.analysis-headline')).toBeNull()
  })

  it('open in chat switches view and scrolls to the answer', async () => {
    const runMessages = [
      { id: 1, role: 'user', content: 'Chart it', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T09:00:00Z' },
      {
        id: 2, role: 'assistant', content: 'First answer.', tool_calls: [{ id: 'c0', function: { name: 'python' } }],
        tool_call_id: null, created_at: '2026-10-27T09:00:01Z',
      },
      { id: 3, role: 'tool', content: '{}', tool_calls: null, tool_call_id: 'c0', created_at: '2026-10-27T09:00:02Z', runs: ['r0'] },
      { id: 4, role: 'user', content: 'Again', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T10:00:00Z' },
      {
        id: 5, role: 'assistant', content: 'Second answer.', tool_calls: [{ id: 'c1', function: { name: 'python' } }],
        tool_call_id: null, created_at: '2026-10-27T10:00:01Z',
      },
      { id: 6, role: 'tool', content: '{}', tool_calls: null, tool_call_id: 'c1', created_at: '2026-10-27T10:00:02Z', runs: ['r1'] },
      { id: 7, role: 'user', content: 'Thanks', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T11:00:00Z' },
    ]
    const summary = (id: string, title: string) => ({ id, title, think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null })
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    routes({
      'GET /api/chats': { body: [summary(CHAT_B, 'Bank statement'), summary(CHAT_A, 'Shop sales')] },
      [`GET /api/chats/${CHAT_A}/messages`]: { body: runMessages },
      [`GET /api/chats/${CHAT_A}/files`]: { body: { files: [] } },
      'GET /api/runs/r0': { status: 404, body: { reason: 'not_found' } },
      'GET /api/runs/r1': { status: 404, body: { reason: 'not_found' } },
    })
    render(<AppShell initialView="analysis" onSignOut={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Open Sales by week full size' }))
    const dialog = await screen.findByRole('dialog', { name: 'Sales by week' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Open in chat' }))

    // Chat opens on the chat that made the chart (not the most recent one), at that turn.
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument())
    const answer = await screen.findByText('Second answer.')
    const turn = answer.closest('.ai') as HTMLElement
    await waitFor(() => expect(turn).toHaveFocus())
    expect(scroll).toHaveBeenCalled()
    expect(scroll.mock.contexts.at(-1)).toBe(turn)
    expect(screen.queryByRole('dialog', { name: 'Sales by week' })).toBeNull()
    expect(screen.getAllByText('Shop sales').length).toBeGreaterThan(0)
  })

  it('open in chat opens the chat at the bottom when the turn is not there', async () => {
    const messages = [
      { id: 1, role: 'user', content: 'Hello', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T09:00:00Z' },
      { id: 2, role: 'assistant', content: 'Hi there.', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T09:00:01Z' },
    ]
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    routes({
      'GET /api/chats': { body: [{ id: CHAT_A, title: 'Shop sales', think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }] },
      [`GET /api/chats/${CHAT_A}/messages`]: { body: messages },
    })
    render(<AppShell initialView="analysis" onSignOut={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open Sales by week full size' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Open in chat' }))
    expect(await screen.findByText('Hi there.')).toBeInTheDocument()
    expect(scroll).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument()
  })

  it('deleting a file from the gallery asks for a passkey', async () => {
    const { calls } = routes({ 'DELETE /api/files/f1': { body: { deleted: { id: 'f1', chat_id: CHAT_A } } } })
    render(<Analysis onOpenChat={() => {}} />)
    const shop = await screen.findByRole('region', { name: 'Shop sales' })
    fireEvent.click(within(shop).getByRole('button', { name: 'Delete sales.csv' }))

    const dialog = screen.getByRole('dialog', { name: 'Delete sales.csv?' })
    expect(stepUp).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete with passkey' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('delete', undefined, 'file:f1')
    const del = calls.find((c) => c.init.method === 'DELETE')!
    expect(del.path).toBe('/api/files/f1')
    expect(bodyOf(del)).toEqual({ step_up_token: 'delete-token' })
    expect(screen.queryByText('sales.csv')).toBeNull()
    expect(screen.getByText('costs.csv')).toBeInTheDocument()

  })

  it('the last file deleted takes its chat out of the gallery', async () => {
    routes({ 'DELETE /api/files/f3': { body: { deleted: { id: 'f3', chat_id: CHAT_B } } } })
    render(<Analysis onOpenChat={() => {}} />)
    const bank = await screen.findByRole('region', { name: 'Bank statement' })
    fireEvent.click(within(bank).getByRole('button', { name: 'Delete bank.pdf' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete with passkey' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Bank statement' })).toBeNull())
    expect(screen.getByRole('region', { name: 'Shop sales' })).toBeInTheDocument()
  })

  it('analysis is a bottom tab next to images', () => {
    mockFetch({ 'GET /api/health': healthy })
    render(<AppShell onSignOut={() => {}} />)
    const tabs = screen.getByRole('navigation', { name: 'Tabs' })
    expect(within(tabs).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'Today',
      'Chat',
      'Images',
      'Analysis',
      'More',
    ])
    fireEvent.click(within(tabs).getByRole('button', { name: 'More' }))
    const sheet = screen.getByRole('dialog', { name: 'More' })
    expect(within(sheet).getByRole('button', { name: 'Files' })).toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: 'Analysis' })).toBeNull()
  })
})
