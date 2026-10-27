import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { NumberMark, RunDetail } from '../api/runs'
import { FindingsCard } from '../components/FindingsCard'
import { Markdown } from '../components/Markdown'
import { Chat } from '../views/Chat'
import { applyEvent, itemsFromMessages, liveTurn, type AssistantItem, type ServerMessage } from '../views/chatModel'
import { mockFetch } from '../test/fetch'
import { emit, FakeEventSource, installEventSource } from '../test/eventsource'

const embed = vi.hoisted(() => vi.fn())
vi.mock('vega-embed', () => ({ default: embed }))

const SANDBOX_FACTS = 'ran in a sandbox · no internet · read-only files'
const MARK_LEGEND =
  'Underlined numbers: green = computed by code, blue = read from a page. Tap a blue or Unverified number for details.'

function run(extra: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 'r1',
    chat_id: 'c1',
    turn_id: 't1',
    purpose: 'analysis',
    status: 'ok',
    error: null,
    duration_ms: 2400,
    script: "import hk\ndf = hk.read('f1')\nprint(len(df))",
    stdout: '4812\n',
    stdout_truncated: false,
    values: [],
    cleaning: ['Dates were in two formats; read both as day first.'],
    sections: [],
    charts: [
      {
        id: 'o1',
        title: 'Sales by week',
        spec: { mark: 'line', data: { values: [{ w: 1, v: 2 }] }, encoding: {} },
      },
    ],
    tables: [
      {
        id: 'o2',
        name: 'Negative amounts',
        columns: ['date', 'amount'],
        rows: [
          ['11 Mar', -240],
          ['25 Mar', -75],
        ],
        row_numbers: [4812, 4977],
        note: null,
        total_rows: 2,
      },
    ],
    files: [{ id: 'o3', name: 'cleaned.csv', mime: 'text/csv', size: 2048 }],
    created_at: '2026-10-27T10:00:00Z',
    ...extra,
  }
}

function turn(text: string, extra: Partial<AssistantItem> = {}): AssistantItem {
  return {
    ...liveTurn('t1', 0),
    state: 'done',
    text,
    steps: [{ id: 's1', tool: 'python', summary: 'Run Python', state: 'ok', runs: ['r1'] }],
    ...extra,
  }
}

function mark(text: string, source: string, kind: NumberMark['kind'], extra: Partial<NumberMark> = {}): NumberMark {
  const start = source.indexOf(text)
  return { start, end: start + text.length, text, kind, page: null, note: null, ...extra }
}

beforeEach(() => {
  installEventSource()
  embed.mockReset()
  embed.mockImplementation(async (el: HTMLElement) => {
    el.innerHTML = '<svg data-chart="yes"></svg>'
    return { view: { toImageURL: vi.fn() }, finalize: vi.fn() }
  })
})

describe('FindingsCard', () => {
  it('findings card shows headline charts tables and a folded how i got this', async () => {
    mockFetch({
      'GET /api/chats': { body: [] },
      'POST /api/chats': { status: 201, body: { id: 'c1', title: '', think: false, created_at: '', active_turn: null } },
      'POST /api/chats/c1/messages': { status: 202, body: { turn_id: 't1' } },
      'GET /api/runs/r1': { body: run() },
    })
    render(<Chat model="Local model 12B" />)
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    fireEvent.change(box, { target: { value: 'Why did sales drop in March?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    emit('step_started', 1, { step_id: 's1', tool: 'python', summary: 'Run Python' })
    emit('attachments', 2, { step_id: 's1', items: [{ kind: 'run', id: 'r1' }] })
    emit('step_finished', 3, { step_id: 's1', ok: true, preview: '' })
    emit('text_delta', 4, { text: 'March sales fell **31%**.' })
    emit('turn_done', 5)

    const card = await screen.findByRole('region', { name: 'Findings' })
    // 1. The headline.
    expect(within(card).getByTestId('assistant-text')).toHaveTextContent('March sales fell 31%.')
    // 2. The charts, embedded once the chart runtime has loaded.
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1))
    expect(within(card).getByRole('figure', { name: 'Sales by week' })).toBeInTheDocument()
    // 3. Cleaned or assumed.
    const cleaned = within(card).getByRole('heading', { name: 'Cleaned or assumed' }).parentElement!
    expect(within(cleaned).getByText('Dates were in two formats; read both as day first.')).toBeInTheDocument()
    // 4. The problem table, with the file's row numbers.
    const table = within(card).getByRole('table', { name: 'Negative amounts' })
    expect(within(table).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['Row', 'date', 'amount'])
    expect(within(table).getByRole('cell', { name: '4812' })).toBeInTheDocument()
    expect(within(table).getByRole('cell', { name: '-240' })).toBeInTheDocument()
    // 5. Output files.
    expect(within(card).getByRole('link', { name: 'Download cleaned.csv' })).toHaveAttribute(
      'href',
      '/api/outputs/o3/download',
    )
    // 6. How I got this, folded.
    const how = within(card).getByText('How I got this').closest('details')!
    expect(how).not.toHaveAttribute('open')
    // In the order the owner chose.
    const order = ['.findings-headline', '.findings-charts', '.findings-cleaning', '.findings-tables', '.findings-files', '.how']
    const found = order.map((sel) => card.querySelector(sel)!)
    found.forEach((el) => expect(el).not.toBeNull())
    for (let i = 1; i < found.length; i++) {
      expect(found[i - 1].compareDocumentPosition(found[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })

  it('at a glance header when profile ran', () => {
    render(<FindingsCard item={turn('3 columns, 120 rows.')} runs={[run({ sections: ['glance'] })]} />)
    expect(screen.getByRole('heading', { name: 'At a glance' })).toBeInTheDocument()
  })

  it('no at a glance header for an ordinary answer', () => {
    render(<FindingsCard item={turn('3 columns.')} runs={[run()]} />)
    expect(screen.queryByRole('heading', { name: 'At a glance' })).toBeNull()
  })

  it('download rows links to the table csv', () => {
    render(
      <FindingsCard
        item={turn('Two rows.')}
        runs={[run({ tables: [{ ...run().tables[0], total_rows: 1200, rows: [['1 Mar', 1]], row_numbers: [] }] })]}
      />,
    )
    const link = screen.getByRole('link', { name: 'Download rows' })
    expect(link).toHaveAttribute('href', '/api/outputs/o2/download')
    expect(link).toHaveAttribute('download')
    expect(screen.getByText('Showing 1 of 1200 rows')).toBeInTheDocument()
    // A reshaped table has no row numbers: no row column at all.
    const table = screen.getByRole('table', { name: 'Negative amounts' })
    expect(within(table).getAllByRole('columnheader').map((th) => th.textContent)).toEqual(['date', 'amount'])
  })

  it('how i got this shows script output time and the exact sandbox facts', () => {
    render(<FindingsCard item={turn('Done.')} runs={[run({ stdout_truncated: true })]} />)
    const how = screen.getByText('How I got this').closest('details')!
    expect(within(how).getByText(/df = hk\.read\('f1'\)/)).toBeInTheDocument()
    expect(within(how).getByText('4812', { selector: 'code' })).toBeInTheDocument()
    expect(within(how).getByText('Printed output was cut at 256 KB.')).toBeInTheDocument()
    expect(within(how).getByText('2.4 s')).toBeInTheDocument()
    expect(within(how).getByText(SANDBOX_FACTS)).toBeInTheDocument()
  })

  it('vision runs say how the page was read', () => {
    render(
      <FindingsCard
        item={turn('Total due £4,210.50.')}
        runs={[run({ purpose: 'vision', script: null, stdout: 'Page 2: Total due 4,210.50', charts: [], tables: [], files: [], cleaning: [] })]}
      />,
    )
    const how = screen.getByText('How I got this').closest('details')!
    expect(within(how).getByText("Read with the chat model's vision · no internet")).toBeInTheDocument()
    expect(within(how).getByText('Page 2: Total due 4,210.50')).toBeInTheDocument()
    // No sandbox ran for a vision read.
    expect(within(how).queryByText(SANDBOX_FACTS)).toBeNull()
  })

  it('unverified number is marked with its note', () => {
    const text = 'March sales fell 31%, about 40 orders.'
    const marks = [
      mark('31%', text, 'computed'),
      mark('40', text, 'unverified', { note: 'Not found in any result.' }),
    ]
    render(<FindingsCard item={turn(text, { check: { text, marks } })} runs={[run()]} />)
    const computed = screen.getByText('31%', { selector: '.num' })
    expect(computed.tagName).toBe('SPAN')
    expect(computed).toHaveClass('num-computed')
    expect(computed).toHaveAttribute('title', 'Computed by code that ran on your file')
    expect(computed).toHaveAccessibleDescription('Computed by code that ran on your file')
    const unverified = screen.getByRole('button', { name: '40' })
    expect(unverified).toHaveClass('num', 'num-unverified')
    expect(unverified).toHaveAttribute('aria-expanded', 'false')
    expect(unverified).toHaveAccessibleDescription('Unverified: Not found in any result.')
    expect(screen.getByText('Unverified: Not found in any result.')).not.toBeVisible()
    fireEvent.click(unverified)
    expect(unverified).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Unverified: Not found in any result.')).toBeVisible()
    expect(screen.getByText("Numbers marked Unverified weren't found in any result.")).toBeInTheDocument()
    expect(screen.getByText(MARK_LEGEND)).toBeInTheDocument()
  })

  it('document number shows read from page', () => {
    const text = 'Total due £4,210.50 and fee 12.'
    const marks = [mark('4,210.50', text, 'document', { page: 2 }), mark('12', text, 'document')]
    render(<FindingsCard item={turn(text, { check: { text, marks } })} runs={[run()]} />)
    const page = screen.getByRole('button', { name: '4,210.50' })
    expect(page).toHaveClass('num-document')
    expect(screen.getByText('read from page 2, not computed')).not.toBeVisible()
    fireEvent.click(page)
    expect(screen.getByText('read from page 2, not computed')).toBeVisible()
    fireEvent.click(page)
    expect(screen.getByText('read from page 2, not computed')).not.toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '12' }))
    expect(screen.getByText('read from the document, not computed')).toBeVisible()
    expect(screen.queryByText("Numbers marked Unverified weren't found in any result.")).toBeNull()
  })

  it('copying a reply gives the plain numbers', () => {
    const text = 'Fell 31% on page total 9.'
    const marks = [mark('31%', text, 'computed'), mark('9', text, 'document', { page: 3 })]
    render(<FindingsCard item={turn(text, { check: { text, marks } })} runs={[run()]} />)
    // The numbers hold only themselves; their notes sit beside them, out of the selection.
    expect(screen.getByText('31%', { selector: '.num' }).textContent).toBe('31%')
    expect(screen.getByRole('button', { name: '9' }).textContent).toBe('9')
    expect(document.querySelector('.num .num-note')).toBeNull()
    expect(document.querySelector('.num-note')).not.toBeNull()
  })

  it('no legend without marks', () => {
    render(<FindingsCard item={turn('Fell 31%.')} runs={[run()]} />)
    expect(screen.queryByText(MARK_LEGEND)).toBeNull()
  })

  it('marks are ignored when the text differs', () => {
    const checked = 'March sales fell 31%.'
    const marks = [mark('31%', checked, 'unverified', { note: 'x' })]
    render(<FindingsCard item={turn('March sales fell 31%!', { check: { text: checked, marks } })} runs={[run()]} />)
    expect(document.querySelector('.num')).toBeNull()
    expect(screen.getByTestId('assistant-text')).toHaveTextContent('March sales fell 31%!')
    expect(screen.queryByText("Numbers marked Unverified weren't found in any result.")).toBeNull()
  })

  it('failed run shows what went wrong', () => {
    render(
      <FindingsCard
        item={turn("The script couldn't finish.")}
        runs={[run({ status: 'timeout', error: 'The run took longer than 120 seconds and was stopped.', charts: [], tables: [], files: [] })]}
      />,
    )
    const how = screen.getByText('How I got this').closest('details')!
    expect(within(how).getByText('The run took longer than 120 seconds and was stopped.')).toBeInTheDocument()
  })

  it('plain turns without runs render as before', async () => {
    mockFetch({
      'GET /api/chats': { body: [{ id: 'c1', title: 'Hi', think: false, created_at: '', active_turn: null }] },
      'GET /api/chats/c1/messages': {
        body: [
          { id: 1, role: 'user', content: 'Hello', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T10:00:00Z' },
          { id: 2, role: 'assistant', content: 'Hi **there**.', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T10:00:01Z', number_check: null },
        ],
      },
    })
    render(<Chat model="Local model 12B" />)
    const reply = await screen.findByTestId('assistant-text')
    expect(reply).toHaveTextContent('Hi there.')
    expect(screen.queryByRole('region', { name: 'Findings' })).toBeNull()
    expect(screen.queryByText('How I got this')).toBeNull()
    expect(embed).not.toHaveBeenCalled()
  })
})

describe('number marks in the thread', () => {
  it('the number_check event is kept on the turn', () => {
    const text = 'Fell 31%.'
    const marks = [mark('31%', text, 'computed')]
    let item = applyEvent(liveTurn('t1', 0), { type: 'text_delta', seq: 1, text }, 0)
    item = applyEvent(item, { type: 'number_check', seq: 2, text, marks }, 0)
    expect(item.check).toEqual({ text, marks })
  })

  it('a saved reply brings its number check back', () => {
    const text = 'Fell 31%.'
    const marks = [mark('31%', text, 'computed')]
    const messages: ServerMessage[] = [
      { id: 1, role: 'user', content: 'Why?', tool_calls: null, tool_call_id: null, created_at: '2026-10-27T10:00:00Z' },
      { id: 2, role: 'assistant', content: text, tool_calls: null, tool_call_id: null, created_at: '2026-10-27T10:00:01Z', number_check: { text, marks } },
    ]
    const item = itemsFromMessages(messages)[1] as AssistantItem
    expect(item.check).toEqual({ text, marks })
  })

  it('marks use UTF-16 offsets and never open markup', () => {
    const text = '😀 <b>12</b> and 7 \uE000\uE1007\uE001'
    const marks = [mark('12', text, 'computed'), mark('7', text, 'unverified', { note: '<img src=x>' })]
    const { container } = render(<Markdown text={text} marks={marks} />)
    expect(screen.getByText('12', { selector: '.num' })).toHaveClass('num-computed')
    expect(screen.getByRole('button', { name: '7' })).toHaveClass('num-unverified')
    expect(container.querySelector('b, img')).toBeNull()
    // Sentinel look-alikes already in the text are not marks.
    expect(container.querySelectorAll('.num')).toHaveLength(2)
    expect(container.textContent).not.toMatch(/[\uE000-\uF8FF]/)
  })

  it('a mark whose text does not match its offsets is skipped', () => {
    const text = 'Fell 31%.'
    render(<Markdown text={text} marks={[{ start: 0, end: 4, text: '31%', kind: 'computed', page: null, note: null }]} />)
    expect(document.querySelector('.num')).toBeNull()
    expect(screen.getByText('Fell 31%.')).toBeInTheDocument()
  })

  it("a numbered list's own numbers stay list markers", () => {
    const text = 'Two causes:\n1. North fell 31%.\n2. Refunds.'
    const marks = [mark('1', text, 'computed'), mark('31%', text, 'computed'), mark('2', text, 'unverified', { note: 'n' })]
    render(<Markdown text={text} marks={marks} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('31%', { selector: '.num' })).toHaveClass('num-computed')
    expect(document.querySelectorAll('.num')).toHaveLength(1)
  })

  it('a number inside a link label is never a button', () => {
    const text = 'See [page 4 total](https://example.org/a) now.'
    render(<Markdown text={text} marks={[mark('4', text, 'document', { page: 4 })]} />)
    const link = screen.getByRole('link', { name: 'page 4 total' })
    expect(link.querySelector('button')).toBeNull()
    expect(link.querySelector('.num-document')).toHaveAttribute('title', 'read from page 4, not computed')
  })

  it('marks inside code stay plain', () => {
    const text = 'Use `x = 31` now.'
    render(<Markdown text={text} marks={[mark('31', text, 'unverified', { note: 'n' })]} />)
    expect(screen.getByText('x = 31').tagName).toBe('CODE')
    expect(document.querySelector('.num')).toBeNull()
  })
})
