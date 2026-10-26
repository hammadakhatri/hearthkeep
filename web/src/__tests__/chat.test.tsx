import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { subscribeTurn, type AgentEvent } from '../api/events'
import { Markdown } from '../components/Markdown'
import { Chat } from '../views/Chat'
import { bodyOf, mockFetch } from '../test/fetch'
import { emit, FakeEventSource, installEventSource, lastSource } from '../test/eventsource'

const newChat = { id: 'c1', title: '', think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }

beforeEach(() => {
  installEventSource()
})

function startChat() {
  const fetches = mockFetch({
    'GET /api/chats': { body: [] },
    'POST /api/chats': { status: 201, body: newChat },
    'POST /api/chats/c1/messages': { status: 202, body: { turn_id: 't1' } },
  })
  render(<Chat model="Local model 12B" />)
  return fetches
}

async function send(text: string) {
  const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
  fireEvent.change(box, { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
}

describe('Chat', () => {
  it('streams text deltas into the assistant message', async () => {
    const { calls } = startChat()
    await send('What time is it?')

    expect(lastSource().url).toBe('/api/chats/c1/turns/t1/events?after=0')
    expect(bodyOf(calls.find((c) => c.path === '/api/chats' && c.init.method === 'POST')!)).toEqual({ think: false })
    expect(bodyOf(calls.find((c) => c.path === '/api/chats/c1/messages')!)).toEqual({ text: 'What time is it?' })
    expect(screen.getByText('What time is it?', { selector: '.me' })).toBeInTheDocument()
    // One turn at a time: typing ahead is fine, sending waits until this reply is done.
    fireEvent.change(screen.getByRole('textbox', { name: 'Message Hearthkeep' }), { target: { value: 'And tomorrow?' } })
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    emit('text_delta', 1, { text: 'It is ' })
    const reply = screen.getByTestId('assistant-text')
    expect(reply).toHaveTextContent('It is')
    emit('text_delta', 2, { text: '**ten** past nine.' })
    expect(reply).toHaveTextContent('It is ten past nine.')
    expect(within(reply).getByText('ten').tagName).toBe('STRONG')

    emit('turn_done', 3)
    expect(lastSource().closed).toBe(true)
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('folds tool steps and shows count', async () => {
    startChat()
    await send('What time is it, and note it down')

    emit('step_started', 1, { step_id: 's1', tool: 'clock', summary: 'Check the time' })
    emit('step_finished', 2, { step_id: 's1', ok: true, preview: '09:10' })
    emit('step_started', 3, { step_id: 's2', tool: 'clock', summary: 'Check the date' })
    emit('step_finished', 4, { step_id: 's2', ok: true, preview: '26 October' })
    emit('text_delta', 5, { text: 'Ten past nine.' })
    emit('turn_done', 6)

    const summary = screen.getByText(/· 2 steps$/)
    expect(summary.textContent).toMatch(/^Worked for \d+ seconds? · 2 steps$/)
    const fold = summary.closest('details')!
    expect(fold).not.toHaveAttribute('open')
    expect(within(fold).getByText('Check the time')).toBeInTheDocument()
    expect(within(fold).getByText('Check the date')).toBeInTheDocument()
    expect(screen.getByTestId('assistant-text')).toHaveTextContent('Ten past nine.')
  })

  it('keeps reasoning behind a Thinking disclosure', async () => {
    startChat()
    await send('Think about it')
    emit('reasoning_delta', 1, { text: 'The owner wants' })
    emit('text_delta', 2, { text: 'Done.' })
    const thinking = screen.getByText('Thinking').closest('details')!
    expect(thinking).not.toHaveAttribute('open')
    expect(within(thinking).getByText('The owner wants')).toBeInTheDocument()
    expect(screen.getByTestId('assistant-text')).not.toHaveTextContent('The owner wants')
  })

  it('shows an approval card inline when the turn needs one', async () => {
    startChat()
    await send('Add a note')
    emit('step_started', 1, { step_id: 's1', tool: 'note', summary: 'Save a note' })
    emit('approval_required', 2, {
      approval_id: 'a1',
      tool: 'note',
      summary: 'Save the note "Buy milk"',
      detail: { tool: 'note', arguments: { text: 'Buy milk' } },
      requires_step_up: false,
    })
    const card = screen.getByRole('group', { name: 'Save the note "Buy milk"' })
    expect(within(card).getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    emit('approval_resolved', 3, { approval_id: 'a1', status: 'declined' })
    expect(within(card).getByText('Declined')).toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('says so when a turn is already running in the chat', async () => {
    mockFetch({
      'GET /api/chats': { body: [] },
      'POST /api/chats': { status: 201, body: newChat },
      'POST /api/chats/c1/messages': { status: 409, body: { reason: 'turn_in_progress' } },
    })
    render(<Chat model="Local model 12B" />)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Message Hearthkeep' }), { target: { value: 'Hi' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Hearthkeep is still answering in this chat.')
  })

  it('resumes a reply that is still running when the chat opens', async () => {
    mockFetch({
      'GET /api/chats': { body: [{ ...newChat, title: 'Time', active_turn: 't9' }] },
      'GET /api/chats/c1/messages': {
        body: [
          { id: 1, role: 'user', content: 'Time?', tool_calls: null, tool_call_id: null, created_at: '2026-10-26T10:00:00Z' },
        ],
      },
    })
    render(<Chat model="Local model 12B" />)
    expect(await screen.findByText('Time?')).toBeInTheDocument()
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(lastSource().url).toBe('/api/chats/c1/turns/t9/events?after=0')
    emit('text_delta', 1, { text: 'Nine.' })
    expect(screen.getByTestId('assistant-text')).toHaveTextContent('Nine.')
  })

  it('shows earlier messages with their tool steps folded', async () => {
    mockFetch({
      'GET /api/chats': { body: [{ ...newChat, title: 'Time' }] },
      'GET /api/chats/c1/messages': {
        body: [
          { id: 1, role: 'user', content: 'Time?', tool_calls: null, tool_call_id: null, created_at: '2026-10-26T10:00:00Z' },
          {
            id: 2,
            role: 'assistant',
            content: '',
            tool_calls: [{ id: 'k1', type: 'function', function: { name: 'clock', arguments: '{}' } }],
            tool_call_id: null,
            created_at: '2026-10-26T10:00:01Z',
          },
          { id: 3, role: 'tool', content: '09:10', tool_calls: null, tool_call_id: 'k1', created_at: '2026-10-26T10:00:02Z' },
          { id: 4, role: 'assistant', content: 'Ten past nine.', tool_calls: null, tool_call_id: null, created_at: '2026-10-26T10:00:04Z' },
        ],
      },
    })
    render(<Chat model="Local model 12B" />)
    expect(await screen.findByText('Ten past nine.')).toBeInTheDocument()
    expect(screen.getByText('Worked for 4 seconds · 1 step')).toBeInTheDocument()
    expect(FakeEventSource.instances).toHaveLength(0)
  })
})

describe('subscribeTurn', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reconnects event stream after drop without duplicating text', () => {
    vi.useFakeTimers()
    const events: AgentEvent[] = []
    const stop = subscribeTurn('c1', 't1', (e) => events.push(e))

    const first = lastSource()
    expect(first.url).toBe('/api/chats/c1/turns/t1/events?after=0')
    first.emit('text_delta', 1, { text: 'Hel' })
    first.emit('text_delta', 2, { text: 'lo' })
    first.fail()
    expect(first.closed).toBe(true)

    vi.advanceTimersByTime(5000)
    const second = lastSource()
    expect(second).not.toBe(first)
    expect(second.url).toBe('/api/chats/c1/turns/t1/events?after=2')
    // A replay that overlaps what was already seen is ignored.
    second.emit('text_delta', 2, { text: 'lo' })
    second.emit('text_delta', 3, { text: ', world' })
    second.emit('turn_done', 4)

    const text = events.flatMap((e) => (e.type === 'text_delta' ? [e.text] : [])).join('')
    expect(text).toBe('Hello, world')
    expect(events.map((e) => e.seq)).toEqual([1, 2, 3, 4])
    expect(second.closed).toBe(true)
    vi.advanceTimersByTime(60_000)
    expect(FakeEventSource.instances).toHaveLength(2)
    stop()
  })

  it('stops for good when unsubscribed', () => {
    vi.useFakeTimers()
    const stop = subscribeTurn('c1', 't1', () => {}, 5)
    expect(lastSource().url).toBe('/api/chats/c1/turns/t1/events?after=5')
    lastSource().fail()
    stop()
    vi.advanceTimersByTime(60_000)
    expect(FakeEventSource.instances).toHaveLength(1)
  })
})

describe('Markdown', () => {
  it('renders the safe subset and never raw HTML', () => {
    const text = [
      'Hello **bold** and *soft* with `code`.',
      '',
      '- one',
      '- two',
      '',
      '1. first',
      '',
      '```',
      '<b>not html</b>',
      '```',
      '',
      'See [the docs](https://example.org/docs) or [this](javascript:alert(1)). <img src=x onerror=alert(1)>',
    ].join('\n')
    const { container } = render(<Markdown text={text} />)
    expect(container.querySelector('strong')).toHaveTextContent('bold')
    expect(container.querySelector('em')).toHaveTextContent('soft')
    expect(container.querySelector('p code')).toHaveTextContent('code')
    expect(container.querySelectorAll('ul li')).toHaveLength(2)
    expect(container.querySelectorAll('ol li')).toHaveLength(1)
    expect(container.querySelector('pre code')).toHaveTextContent('<b>not html</b>')
    expect(container.querySelector('b')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    const links = container.querySelectorAll('a')
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', 'https://example.org/docs')
    expect(links[0]).toHaveAttribute('target', '_blank')
    expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer')
    expect(container).toHaveTextContent('[this](javascript:alert(1))')
  })
})

