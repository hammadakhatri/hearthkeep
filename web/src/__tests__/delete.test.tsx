import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { ConfirmDelete } from '../components/ConfirmDelete'
import { Chat } from '../views/Chat'
import { bodyOf, mockFetch, type Reply } from '../test/fetch'
import { installEventSource } from '../test/eventsource'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const CHAT_A = 'a'.repeat(32)
const CHAT_B = 'b'.repeat(32)

function chat(id: string, title: string) {
  return { id, title, think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }
}

function userMessage(id: number, content: string) {
  return { id, role: 'user', content, tool_calls: null, tool_call_id: null, created_at: '2026-10-26T10:00:00Z' }
}

const bankInfo = {
  id: 'f1',
  chat_id: CHAT_A,
  name: 'bank.csv',
  kind: 'csv',
  size: 2048,
  status: 'ready',
  created_at: '2026-10-26T10:00:00Z',
}

function routes(extra: Record<string, Reply | ((init: RequestInit) => Reply | Promise<Reply>)> = {}) {
  return mockFetch({
    'GET /api/chats': { body: [chat(CHAT_A, 'Bank statement'), chat(CHAT_B, 'Holiday plans')] },
    [`GET /api/chats/${CHAT_A}/messages`]: { body: [userMessage(1, 'Sum it\nAttached file: f1')] },
    [`GET /api/chats/${CHAT_B}/messages`]: { body: [] },
    [`GET /api/chats/${CHAT_A}/files`]: { body: { files: [bankInfo] } },
    ...extra,
  })
}

function chatList(): HTMLElement {
  return screen.getAllByRole('complementary', { name: 'Chats' })[0]
}

beforeEach(() => {
  installEventSource()
  vi.mocked(stepUp).mockReset().mockResolvedValue('delete-token')
})

describe('deleting a chat', () => {
  it('delete chat asks for a passkey then removes it from the list', async () => {
    const { calls } = routes({ [`DELETE /api/chats/${CHAT_A}`]: { body: { deleted: { chats: 1 } } } })
    render(<Chat model="Local model 12B" />)
    await screen.findByText('Sum it', { selector: '.me' })
    const trigger = within(chatList()).getByRole('button', { name: 'Delete chat Bank statement' })
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: 'Delete this chat?' })
    expect(dialog).toHaveAccessibleDescription(
      "This removes its messages, files, analysis, images and anything sent from it. It can't be undone.",
    )
    expect(stepUp).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete with passkey' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('delete', undefined, `chat:${CHAT_A}`)
    const sent = calls.find((c) => c.init.method === 'DELETE')!
    expect(sent.path).toBe(`/api/chats/${CHAT_A}`)
    expect(bodyOf(sent)).toEqual({ step_up_token: 'delete-token' })
    expect(within(chatList()).queryByText('Bank statement')).not.toBeInTheDocument()
    expect(within(chatList()).getByText('Holiday plans')).toBeInTheDocument()
    // It was the open chat: a new, empty one takes its place.
    expect(screen.getByRole('heading', { name: 'What can I help with?' })).toBeInTheDocument()
    expect(screen.queryByText('Sum it', { selector: '.me' })).not.toBeInTheDocument()
  })

  it('cancel keeps the chat', async () => {
    const { calls } = routes()
    render(<Chat model="Local model 12B" />)
    await screen.findByText('Sum it', { selector: '.me' })
    const trigger = within(chatList()).getByRole('button', { name: 'Delete chat Holiday plans' })
    trigger.focus()
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Delete this chat?' })
    // Focus starts inside and stays inside.
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' })
    const confirm = within(dialog).getByRole('button', { name: 'Delete with passkey' })
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    confirm.focus()
    fireEvent.keyDown(confirm, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()

    fireEvent.click(cancel)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()

    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()

    expect(stepUp).not.toHaveBeenCalled()
    expect(calls.some((c) => c.init.method === 'DELETE')).toBe(false)
    expect(within(chatList()).getByText('Holiday plans')).toBeInTheDocument()
  })

  it('opens from a long press on a chat', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      routes()
      render(<Chat model="Local model 12B" />)
      const link = await within(chatList()).findByRole('button', { name: 'Holiday plans' })
      fireEvent.pointerDown(link, { pointerType: 'touch' })
      act(() => vi.advanceTimersByTime(600))
      expect(screen.getByRole('dialog', { name: 'Delete this chat?' })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('opens from the context menu, and says why a busy chat stays', async () => {
    routes({ [`DELETE /api/chats/${CHAT_B}`]: { status: 409, body: { reason: 'turn_in_progress' } } })
    render(<Chat model="Local model 12B" />)
    const link = await within(chatList()).findByRole('button', { name: 'Holiday plans' })
    fireEvent.contextMenu(link)
    const dialog = screen.getByRole('dialog', { name: 'Delete this chat?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete with passkey' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Hearthkeep is still answering in this chat. Wait for it to finish, then try again.',
    )
    expect(within(chatList()).getByText('Holiday plans')).toBeInTheDocument()
  })

  it('a cancelled passkey keeps the chat and says so', async () => {
    vi.mocked(stepUp).mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    const { calls } = routes()
    render(<Chat model="Local model 12B" />)
    fireEvent.click(await within(chatList()).findByRole('button', { name: 'Delete chat Holiday plans' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete with passkey' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Delete was cancelled.')
    expect(calls.some((c) => c.init.method === 'DELETE')).toBe(false)
  })
})

describe('deleting a file', () => {
  it('delete file chip removes it after a passkey', async () => {
    const { calls } = routes({ 'DELETE /api/files/f1': { body: { deleted: { id: 'f1', chat_id: CHAT_A } } } })
    render(<Chat model="Local model 12B" />)
    const chips = await screen.findByRole('list', { name: 'Files you sent in message 1' })
    const remove = await within(chips).findByRole('button', { name: 'Delete bank.csv' })
    fireEvent.click(remove)

    const dialog = screen.getByRole('dialog', { name: 'Delete bank.csv?' })
    expect(dialog).toHaveAccessibleDescription(
      "This removes the file from this chat. Answers already given keep their results. It can't be undone.",
    )
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete with passkey' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('delete', undefined, 'file:f1')
    const sent = calls.find((c) => c.init.method === 'DELETE')!
    expect(sent.path).toBe('/api/files/f1')
    expect(bodyOf(sent)).toEqual({ step_up_token: 'delete-token' })
    expect(screen.queryByRole('list', { name: 'Files you sent in message 1' })).not.toBeInTheDocument()
    expect(screen.getByText('Sum it', { selector: '.me' })).toBeInTheDocument()
  })

  it('composer chips keep a plain Remove with no passkey', async () => {
    routes({
      'GET /api/chats': { body: [] },
      'POST /api/chats': { status: 201, body: chat(CHAT_A, '') },
      [`POST /api/chats/${CHAT_A}/files`]: { status: 201, body: bankInfo },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
    fireEvent.change(input, { target: { files: [new File(['a,b\n1,2\n'], 'bank.csv', { type: 'text/csv' })] } })
    const composer = await screen.findByRole('list', { name: 'Files attached to this message' })
    await within(composer).findByRole('link', { name: 'Download bank.csv' })
    expect(within(composer).queryByRole('button', { name: 'Delete bank.csv' })).not.toBeInTheDocument()
    fireEvent.click(within(composer).getByRole('button', { name: 'Remove bank.csv' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(stepUp).not.toHaveBeenCalled()
  })
})

describe('ConfirmDelete', () => {
  it('is a labelled dialog with the two buttons', () => {
    const onCancel = vi.fn()
    render(<ConfirmDelete title="Delete x?" body="Gone for good." onConfirm={async () => {}} onCancel={onCancel} />)
    const dialog = screen.getByRole('dialog', { name: 'Delete x?' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAccessibleDescription('Gone for good.')
    expect(within(dialog).getAllByRole('button').map((b) => b.textContent)).toEqual(['Cancel', 'Delete with passkey'])
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
