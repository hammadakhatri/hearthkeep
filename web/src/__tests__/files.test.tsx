import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  FILE_ACCEPT,
  attachedFileLine,
  formatSize,
  isPhotoFile,
  listChatFiles,
  splitAttachments,
} from '../api/files'
import { applyEvent, itemsFromMessages, liveTurn, type AssistantItem } from '../views/chatModel'
import { Chat } from '../views/Chat'
import { bodyOf, headerOf, mockFetch, type Reply } from '../test/fetch'
import { emit, FakeEventSource, installEventSource } from '../test/eventsource'

const chatC1 = { id: 'c1', title: '', think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }

function info(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    chat_id: 'c1',
    name,
    kind: 'csv',
    size: 839_680,
    status: 'ready',
    created_at: '2026-10-26T10:00:00Z',
    ...extra,
  }
}

const csv = (name = 'bank.csv') => new File(['a,b\n1,2\n'], name, { type: 'text/csv' })
const png = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'me.png', { type: 'image/png' })

function fileInput(): HTMLInputElement {
  return document.querySelector<HTMLInputElement>('input[type="file"]')!
}

function pick(...files: File[]) {
  fireEvent.change(fileInput(), { target: { files } })
}

/** The composer's chips (the chat list has list items, too). */
function chips(): HTMLElement[] {
  const list = screen.queryByRole('list', { name: 'Files attached to this message' })
  return list ? within(list).queryAllByRole('listitem') : []
}

/** The visible "up to 5" note (the live region says it too, but unseen). */
function notice(): HTMLElement | null {
  return document.querySelector('.composer-notice')
}

function announcer(): HTMLElement {
  return screen.getByTestId('photo-status')
}

function userMessage(id: number, content: string) {
  return { id, role: 'user', content, tool_calls: null, tool_call_id: null, created_at: '2026-10-26T10:00:00Z' }
}

function routes(extra: Record<string, Reply | ((init: RequestInit) => Reply | Promise<Reply>)> = {}) {
  return mockFetch({
    'GET /api/chats': { body: [] },
    'POST /api/chats': { status: 201, body: chatC1 },
    'POST /api/chats/c1/files': { status: 201, body: info('f1', 'bank.csv') },
    'POST /api/chats/c1/messages': { status: 202, body: { turn_id: 't1' } },
    ...extra,
  })
}

beforeEach(() => {
  installEventSource()
})

describe('file helpers', () => {
  it('formats sizes', () => {
    expect(formatSize(839_680)).toBe('820 KB')
    expect(formatSize(2.4 * 1024 * 1024)).toBe('2.4 MB')
    expect(formatSize(512)).toBe('512 B')
  })

  it('splits photo and file lines from the words', () => {
    expect(
      splitAttachments('Look at these\nAttached photo: p1\nAttached file: f1\nAttached file: f 2\n Attached file: f3'),
    ).toEqual({ photos: ['p1'], files: ['f1'], text: 'Look at these\nAttached file: f 2\n Attached file: f3' })
    expect(attachedFileLine('abc')).toBe('Attached file: abc')
  })

  it('normalises CRLF and CR line ends before splitting', () => {
    expect(splitAttachments('hi\r\nAttached file: f1\rAttached photo: p1\r\n')).toEqual({
      photos: ['p1'],
      files: ['f1'],
      text: 'hi',
    })
  })

  it('routes photo types to the photo path and offers every kind', () => {
    expect(isPhotoFile(png)).toBe(true)
    expect(isPhotoFile(new File([''], 'x.WEBP'))).toBe(true)
    expect(isPhotoFile(csv())).toBe(false)
    expect(isPhotoFile(new File([''], 'x.pdf', { type: 'application/pdf' }))).toBe(false)
    for (const part of ['.csv', '.xlsx', '.xls', '.pdf', '.txt', '.json', 'image/png', 'image/jpeg', 'image/webp']) {
      expect(FILE_ACCEPT.split(',')).toContain(part)
    }
  })

  it('lists the files of a chat from the server wrapper', async () => {
    mockFetch({ 'GET /api/chats/c1/files': { body: { files: [info('f1', 'a.csv')] } } })
    expect((await listChatFiles('c1')).map((f) => f.id)).toEqual(['f1'])
  })
})

describe('attaching files', () => {
  it('attach accepts files and shows chips with name type and size', async () => {
    routes({ 'POST /api/chats/c1/files': { status: 201, body: info('f1', 'bank.csv', { size: 839_680 }) } })
    render(<Chat model="Local model 12B" />)
    const clip = await screen.findByRole('button', { name: 'Attach a photo or file' })
    expect(clip).toBeEnabled()
    expect(fileInput().multiple).toBe(true)

    pick(csv())
    const list = await screen.findByRole('list', { name: 'Files attached to this message' })
    await waitFor(() => expect(within(list).getByText('CSV')).toBeInTheDocument())
    const chip = within(list).getByRole('listitem')
    expect(chip).toHaveTextContent('bank.csv')
    expect(chip).toHaveTextContent('820 KB')
    expect(announcer()).toHaveTextContent('bank.csv attached')
    expect(within(chip).getByRole('button', { name: 'Remove bank.csv' })).toBeInTheDocument()
  })

  it('shows a spinner while uploading and holds Send until it is done', async () => {
    let finish!: (reply: Reply) => void
    routes({ 'POST /api/chats/c1/files': () => new Promise<Reply>((resolve) => (finish = resolve)) })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Hearthkeep' }), { target: { value: 'Summarise' } })
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()

    pick(csv())
    const chip = (await waitFor(() => (expect(chips()).toHaveLength(1), chips()[0])))
    await waitFor(() => expect(announcer()).toHaveTextContent('Uploading bank.csv'))
    expect(chip).toHaveTextContent('bank.csv')
    expect(chip).toHaveAttribute('data-state', 'uploading')
    expect(chip.querySelector('.file-chip-wait')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    await act(async () => finish({ status: 201, body: info('f1', 'bank.csv') }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled())
  })

  it('creates the chat before the first upload', async () => {
    const { calls } = routes()
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    fireEvent.click(screen.getByRole('button', { name: 'Think' }))

    pick(csv('a.csv'), csv('b.csv'))
    await waitFor(() => expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(2))
    const paths = calls.filter((c) => c.init.method === 'POST').map((c) => c.path)
    // One chat, made first with the draft's Think choice; then both uploads go to it.
    expect(paths).toEqual(['/api/chats', '/api/chats/c1/files', '/api/chats/c1/files'])
    const create = calls.find((c) => c.path === '/api/chats' && c.init.method === 'POST')!
    expect(bodyOf(create)).toEqual({ think: true })
    const upload = calls.find((c) => c.path === '/api/chats/c1/files')!
    expect(headerOf(upload, 'X-Hearthkeep')).toBe('1')
    expect(upload.init.body).toBeInstanceOf(FormData)
    expect((upload.init.body as FormData).get('file')).toBeInstanceOf(File)
  })

  it('sends attached file lines with the message', async () => {
    let n = 0
    const { calls } = routes({
      'POST /api/chats/c1/files': () => ({ status: 201, body: info(`f${++n}`, n === 1 ? 'bank.csv' : 'sheet.csv') }),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(csv('bank.csv'), csv('sheet.csv'))
    await waitFor(() => expect(chips()).toHaveLength(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled())

    // Files alone are enough to send.
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/c1/messages')!
    expect(bodyOf(message)).toEqual({ text: 'Attached file: f1\nAttached file: f2' })

    // The bubble shows chips, not the lines; the chat is titled with the first file's name.
    const bubble = document.querySelector('.me') as HTMLElement
    expect(bubble).not.toHaveTextContent('Attached file')
    expect(within(bubble).getAllByRole('listitem')).toHaveLength(2)
    expect(document.querySelector('.chat-bar-title')).toHaveTextContent(/^bank\.csv$/)
    // They are with the message now, so the composer's chips are gone.
    expect(screen.queryByRole('button', { name: 'Remove bank.csv' })).toBeNull()
  })

  it('keeps a photo on the photo path alongside files', async () => {
    const { calls } = routes({
      'POST /api/uploads': { status: 201, body: { id: 'up1', width: 10, height: 10 } },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(png, csv())
    await waitFor(() => expect(announcer()).toHaveTextContent('bank.csv attached'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove photo' })).toBeInTheDocument())
    expect(calls.some((c) => c.path === '/api/uploads')).toBe(true)
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Hearthkeep' }), { target: { value: 'Compare' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/c1/messages')!
    expect(bodyOf(message)).toEqual({ text: 'Compare\nAttached photo: up1\nAttached file: f1' })
  })

  it('refuses a sixth file', async () => {
    let n = 0
    const { calls } = routes({
      'POST /api/chats/c1/files': () => ({ status: 201, body: info(`f${++n}`, `file${n}.csv`) }),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(csv('1.csv'), csv('2.csv'), csv('3.csv'), csv('4.csv'), csv('5.csv'))
    await waitFor(() => expect(chips()).toHaveLength(5))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled())

    pick(csv('6.csv'))
    await waitFor(() => expect(notice()).toHaveTextContent('Attach up to 5 files at a time.'))
    // Said once: through the live region, not also as an alert.
    expect(announcer()).toHaveTextContent('Attach up to 5 files at a time.')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(chips()).toHaveLength(5)
    expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(5)

    // Taking one away makes room again.
    fireEvent.click(screen.getByRole('button', { name: 'Remove file1.csv' }))
    expect(announcer()).toHaveTextContent('file1.csv removed')
    expect(notice()).toBeNull()
    pick(csv('7.csv'))
    await waitFor(() => expect(chips()).toHaveLength(5))
  })

  it('shows the server refusal message for a password-protected file', async () => {
    const message = 'This file is password-protected. Remove the password and attach it again.'
    routes({
      'POST /api/chats/c1/files': { status: 422, body: { reason: 'password_protected', message } },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Hearthkeep' }), { target: { value: 'Read it' } })
    pick(new File(['%PDF'], 'statement.pdf', { type: 'application/pdf' }))

    const chip = (await waitFor(() => (expect(chips()).toHaveLength(1), chips()[0])))
    await waitFor(() => expect(chip).toHaveAttribute('data-state', 'failed'))
    expect(within(chip).getByText(message)).toBeInTheDocument()
    expect(announcer()).toHaveTextContent(message)
    // A failed file is not sent; Remove clears it.
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
    fireEvent.click(within(chip).getByRole('button', { name: 'Remove statement.pdf' }))
    expect(chips()).toHaveLength(0)
  })

  it('marks an unchecked file with its tooltip', async () => {
    routes({
      'POST /api/chats/c1/files': { status: 201, body: info('f1', 'odd.xls', { kind: 'xls', status: 'unchecked' }) },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(csv('odd.xls'))
    const note = await screen.findByText('Not checked yet')
    expect(note).toHaveAttribute('title', "Hearthkeep couldn't open it in the sandbox; it can still try when you ask.")
    expect(chips()[0]).toHaveTextContent('Excel')
  })

  it('drops pending files when another chat opens', async () => {
    const other = { ...chatC1, id: 'c2', title: 'Older chat' }
    mockFetch({
      'GET /api/chats': { body: [{ ...chatC1, title: 'Recent chat' }, other] },
      'GET /api/chats/c1/messages': { body: [] },
      'GET /api/chats/c2/messages': { body: [] },
      'POST /api/chats/c1/files': { status: 201, body: info('f1', 'bank.csv') },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Older chat' })
    pick(csv())
    await screen.findByRole('button', { name: 'Remove bank.csv' })
    fireEvent.click(screen.getByRole('button', { name: 'Older chat' }))
    await waitFor(() => expect(chips()).toHaveLength(0))
  })
})

describe('races and edge cases', () => {
  const twoChats = [{ ...chatC1, title: 'Recent chat' }, { ...chatC1, id: 'c2', title: 'Older chat' }]

  it('drops a late upload after the chat changed: no chip, no name, no line', async () => {
    let finish!: (reply: Reply) => void
    const { calls } = mockFetch({
      'GET /api/chats': { body: twoChats },
      'GET /api/chats/c1/messages': { body: [] },
      'GET /api/chats/c2/messages': { body: [userMessage(1, 'Old question\nAttached file: f1')] },
      'GET /api/chats/c2/files': { body: { files: [] } },
      'POST /api/chats/c1/files': () => new Promise<Reply>((resolve) => (finish = resolve)),
      'POST /api/chats/c2/messages': { status: 202, body: { turn_id: 't1' } },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Older chat' })
    pick(csv())
    await waitFor(() => expect(chips()).toHaveLength(1))

    fireEvent.click(screen.getByRole('button', { name: 'Older chat' }))
    await waitFor(() => expect(chips()).toHaveLength(0))
    await screen.findByText('Old question', { exact: false })
    await act(async () => finish({ status: 201, body: info('f1', 'bank.csv') }))

    // Nothing from the late response reaches the new chat: no chip, no learnt name, no Send line.
    expect(chips()).toHaveLength(0)
    const bubble = document.querySelector('.me') as HTMLElement
    expect(within(bubble).queryByText('bank.csv')).toBeNull()
    expect(announcer()).not.toHaveTextContent('bank.csv attached')
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Hearthkeep' }), { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(bodyOf(calls.find((c) => c.path === '/api/chats/c2/messages' && c.init.method === 'POST')!)).toEqual({
      text: 'Hello',
    })
  })

  it('clears pending files when history loads a different chat', async () => {
    let listed!: (reply: Reply) => void
    mockFetch({
      'GET /api/chats': () => new Promise<Reply>((resolve) => (listed = resolve)),
      'POST /api/chats': { status: 201, body: { ...chatC1, id: 'c9' } },
      'POST /api/chats/c9/files': { status: 201, body: info('f1', 'bank.csv', { chat_id: 'c9' }) },
      'GET /api/chats/c2/messages': { body: [] },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    pick(csv())
    await screen.findByRole('button', { name: 'Remove bank.csv' })

    await act(async () => listed({ body: [{ ...chatC1, id: 'c2', title: 'Older chat' }] }))
    await waitFor(() => expect(chips()).toHaveLength(0))
  })

  it('shows the failure on every chip when the chat cannot be made, and a retry makes one chat', async () => {
    let made = 0
    const { calls } = routes({
      'POST /api/chats': () => (++made === 1 ? { status: 500, body: null } : { status: 201, body: chatC1 }),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(csv('a.csv'), csv('b.csv'))
    await waitFor(() => expect(chips().filter((c) => c.dataset.state === 'failed')).toHaveLength(2))
    for (const chip of chips()) expect(chip).toHaveTextContent(/couldn't be uploaded/)
    expect(calls.filter((c) => c.path === '/api/chats' && c.init.method === 'POST')).toHaveLength(1)

    // Retry: remove the failed chips, pick again; both files share a single new chat.
    for (const name of ['a.csv', 'b.csv']) fireEvent.click(screen.getByRole('button', { name: `Remove ${name}` }))
    pick(csv('a.csv'), csv('b.csv'))
    await waitFor(() => expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(2))
    expect(calls.filter((c) => c.path === '/api/chats' && c.init.method === 'POST')).toHaveLength(2)
  })

  it('takes five of a six-file single pick and says so', async () => {
    let n = 0
    const { calls } = routes({
      'POST /api/chats/c1/files': () => ({ status: 201, body: info(`f${++n}`, `file${n}.csv`) }),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(...['1', '2', '3', '4', '5', '6'].map((i) => csv(`${i}.csv`)))
    await waitFor(() => expect(chips()).toHaveLength(5))
    await waitFor(() => expect(notice()).toHaveTextContent('Attach up to 5 files at a time.'))
    expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(5)
  })

  it('ignores a create that was superseded, and a pick after New chat starts its own', async () => {
    const pending: ((reply: Reply) => void)[] = []
    const { calls } = routes({
      'POST /api/chats': () => new Promise<Reply>((resolve) => pending.push(resolve)),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(csv('a.csv'))
    await waitFor(() => expect(pending).toHaveLength(1))
    fireEvent.click(screen.getAllByRole('button', { name: 'New chat' })[0])
    expect(chips()).toHaveLength(0)

    // Not joined to the old create: a second chat is asked for.
    pick(csv('b.csv'))
    await waitFor(() => expect(pending).toHaveLength(2))
    await act(async () => pending[0]({ status: 201, body: chatC1 }))
    // The superseded chat is not opened and nothing is uploaded to it.
    expect(document.querySelector('.chat-bar-title')).toHaveTextContent('New chat')
    expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(0)

    await act(async () => pending[1]({ status: 201, body: chatC1 }))
    await waitFor(() => expect(chips()).toHaveLength(1))
    expect(chips()[0]).toHaveTextContent('bank.csv')
    expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(1)
  })

  it('keeps files picked while a message is being sent', async () => {
    let accept!: (reply: Reply) => void
    let n = 0
    routes({
      'POST /api/chats/c1/files': () => ({ status: 201, body: info(`f${++n}`, `file${n}.csv`) }),
      'POST /api/chats/c1/messages': () => new Promise<Reply>((resolve) => (accept = resolve)),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pick(csv('first.csv'))
    await waitFor(() => expect(chips()).toHaveLength(1))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(accept).toBeDefined())

    pick(csv('second.csv'))
    await waitFor(() => expect(chips()).toHaveLength(2))
    await act(async () => accept({ status: 202, body: { turn_id: 't1' } }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    // Only the file that went with the message is gone.
    expect(chips()).toHaveLength(1)
    expect(chips()[0]).toHaveTextContent('file2.csv')
  })
})

describe('files in the thread', () => {
  it('renders a hostile file name as text and builds the link from the id', async () => {
    const name = '<img src=x onerror=alert(1)>.csv'
    mockFetch({
      'GET /api/chats': { body: [{ ...chatC1, title: 'Taxes' }] },
      'GET /api/chats/c1/messages': { body: [userMessage(1, 'Look\nAttached file: f1')] },
      'GET /api/chats/c1/files': { body: { files: [info('f1', name)] } },
    })
    render(<Chat model="Local model 12B" />)
    const link = await screen.findByRole('link', { name: `Download ${name}` })
    const bubble = document.querySelector('.me') as HTMLElement
    expect(within(bubble).getByText(name)).toBeInTheDocument()
    expect(bubble.querySelector('img')).toBeNull()
    expect(link).toHaveAttribute('href', '/api/files/f1/download')
    // Names are isolated so a right-to-left name can't reorder the text around it.
    expect(bubble.querySelector('bdi')).toHaveTextContent(name)
  })

  it('user bubble shows file chips from attached lines', async () => {
    const { calls } = mockFetch({
      'GET /api/chats': { body: [{ ...chatC1, title: 'Taxes' }] },
      'GET /api/chats/c1/messages': {
        body: [
          userMessage(1, 'What did I spend?\nAttached file: f1\nAttached file: f2\nAttached file: not an id!'),
          userMessage(2, 'And later\nAttached file: f1'),
        ],
      },
      'GET /api/chats/c1/files': {
        body: { files: [info('f1', 'bank.csv'), info('f2', 'notes.pdf', { kind: 'pdf', size: 2.4 * 1024 * 1024 })] },
      },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByText('What did I spend?', { exact: false })
    const bubbles = Array.from(document.querySelectorAll<HTMLElement>('.me'))
    const first = bubbles[0]
    await waitFor(() => expect(within(first).getByText('notes.pdf')).toBeInTheDocument())
    expect(within(first).getByText('PDF')).toBeInTheDocument()
    expect(within(first).getByText('2.4 MB')).toBeInTheDocument()
    expect(within(first).getAllByRole('listitem')).toHaveLength(2)
    // The lines are hidden; anything that isn't an exact file line stays as written.
    expect(first).not.toHaveTextContent('Attached file: f1')
    expect(first).toHaveTextContent('Attached file: not an id!')
    expect(bubbles[1]).toHaveTextContent('bank.csv')
    // Names come from one list request for the chat.
    expect(calls.filter((c) => c.path === '/api/chats/c1/files')).toHaveLength(1)
  })

  it('file chip links to the download', async () => {
    mockFetch({
      'GET /api/chats': { body: [{ ...chatC1, title: 'Taxes' }] },
      'GET /api/chats/c1/messages': { body: [userMessage(1, 'Check it\nAttached file: f1')] },
      'GET /api/chats/c1/files': { body: { files: [info('f1', 'bank.csv')] } },
    })
    render(<Chat model="Local model 12B" />)
    const link = await screen.findByRole('link', { name: 'Download bank.csv' })
    expect(link).toHaveAttribute('href', '/api/files/f1/download')
    expect(link).toHaveAttribute('download')
  })

  it('file attachments show as chips under their step', async () => {
    routes({
      'GET /api/chats/c1/files': { body: { files: [info('out1', 'result.csv')] } },
    })
    render(<Chat model="Local model 12B" />)
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    fireEvent.change(box, { target: { value: 'Analyse it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    emit('step_started', 1, { step_id: 's1', tool: 'python', summary: 'Ran an analysis' })
    emit('attachments', 2, {
      step_id: 's1',
      items: [
        { kind: 'run', id: 'run1' },
        { kind: 'file', id: 'out1' },
        { kind: 'run', id: 'run1' },
      ],
    })
    emit('step_finished', 3, { step_id: 's1', ok: true, preview: 'ok' })
    emit('turn_done', 4)

    // The file shows as a chip under the step, named once the chat's list is fetched.
    expect(await screen.findByRole('link', { name: 'Download result.csv' })).toHaveAttribute(
      'href',
      '/api/files/out1/download',
    )
  })
})

describe('chat model', () => {
  const turn = (): AssistantItem =>
    applyEvent(liveTurn('t1', 0), { type: 'step_started', seq: 1, step_id: 's1', tool: 'python', summary: 'x' }, 0)

  it('run attachments are recorded on the step', () => {
    const next = applyEvent(
      turn(),
      {
        type: 'attachments',
        seq: 2,
        step_id: 's1',
        items: [
          { kind: 'run', id: 'r1' },
          { kind: 'run', id: 'r1' },
          { kind: 'file', id: 'f1' },
          { kind: 'image', id: 'i1' },
        ],
      },
      0,
    )
    expect(next.steps[0]).toMatchObject({ runs: ['r1'], files: ['f1'], images: ['i1'] })
  })

  it('rebuilds the runs of a step from a saved tool message', () => {
    const items = itemsFromMessages([
      userMessage(1, 'Go') as never,
      {
        id: 2,
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call1', function: { name: 'python' } }],
        tool_call_id: null,
        created_at: '2026-10-26T10:00:01Z',
      },
      {
        id: 3,
        role: 'tool',
        content: '{}',
        tool_calls: null,
        tool_call_id: 'call1',
        created_at: '2026-10-26T10:00:02Z',
        runs: ['run1', 'run2'],
      },
    ])
    const turnItem = items[1] as AssistantItem
    expect(turnItem.steps[0].runs).toEqual(['run1', 'run2'])
  })
})
