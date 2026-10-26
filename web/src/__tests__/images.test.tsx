import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { IMAGES_CHAT_KEY, Images, composeMessage } from '../views/Images'
import { bodyOf, mockFetch, type Reply } from '../test/fetch'
import { emit, FakeEventSource, installEventSource, lastSource } from '../test/eventsource'

const PAGE = '/api/images?limit=24'

const models = {
  body: {
    models: [
      { id: 'gemma', name: 'Local model 12B', kind: 'chat', capabilities: [], vram_gb: 9, source: 'card', resident: true, managed: true },
      { id: 'zimage', name: 'Z-Image Turbo', kind: 'image', capabilities: ['text'], vram_gb: 12, source: 'card', resident: false, managed: false },
      { id: 'flux', name: 'FLUX klein', kind: 'image', capabilities: ['edit'], vram_gb: 10, source: 'card', resident: false, managed: false },
    ],
    errors: [],
    gpu: { resident: ['gemma'] },
  },
}

const imagesChat = { id: 'ic1', title: 'Images', think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }

function image(n: number, extra: Record<string, unknown> = {}) {
  return {
    id: `img${n}`,
    model_id: 'zimage',
    prompt: `A prompt for image ${n}`,
    width: 1024,
    height: 1024,
    created_at: `2026-10-26T10:${String(59 - n).padStart(2, '0')}:00Z`,
    ...extra,
  }
}

function routes(extra: Record<string, Reply | ((init: RequestInit) => Reply)> = {}) {
  return mockFetch({
    'GET /api/models': models,
    [`GET ${PAGE}`]: { body: { images: [] } },
    'POST /api/chats': { status: 201, body: imagesChat },
    'POST /api/chats/ic1/messages': { status: 202, body: { turn_id: 't1' } },
    ...extra,
  })
}

async function describeAndCreate(text: string) {
  const box = await screen.findByRole('textbox', { name: 'Describe the image you want' })
  fireEvent.change(box, { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Create' }))
}

beforeEach(() => {
  installEventSource()
  localStorage.clear()
})

afterEach(() => {
  localStorage.clear()
})

describe('composeMessage', () => {
  it('names the count, the shape and, unless Auto, the model', () => {
    expect(composeMessage({ text: 'a red fox', count: 4, shape: 'square', model: null })).toBe(
      'Create 4 image(s), shape square: a red fox',
    )
    expect(composeMessage({ text: 'a red fox', count: 1, shape: 'banner', model: 'FLUX klein' })).toBe(
      'Create 1 image(s), shape banner, using FLUX klein: a red fox',
    )
  })
})

describe('Images', () => {
  it('images view sends the composed message to the Images chat', async () => {
    const { calls } = routes()
    render(<Images />)
    await screen.findByRole('option', { name: 'Z-Image Turbo' })

    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), { target: { value: 'flux' } })
    fireEvent.click(within(screen.getByRole('group', { name: 'Shape' })).getByRole('button', { name: 'Landscape' }))
    fireEvent.click(within(screen.getByRole('group', { name: 'How many' })).getByRole('button', { name: '2' }))
    await describeAndCreate('  A lighthouse at dusk  ')

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const create = calls.find((c) => c.path === '/api/chats' && c.init.method === 'POST')!
    expect(bodyOf(create)).toEqual({ think: false, title: 'Images' })
    const message = calls.find((c) => c.path === '/api/chats/ic1/messages')!
    expect(bodyOf(message)).toEqual({ text: 'Create 2 image(s), shape landscape, using FLUX klein: A lighthouse at dusk' })
    expect(lastSource().url).toBe('/api/chats/ic1/turns/t1/events?after=0')

    // The live tool step, then the new images as they arrive, prepended to the gallery.
    emit('step_started', 1, { step_id: 's1', tool: 'generate_image', summary: 'Create 2 image(s) with FLUX klein' })
    expect(screen.getByText('Create 2 image(s) with FLUX klein')).toBeInTheDocument()
    emit('attachments', 2, { step_id: 's1', items: [{ kind: 'image', id: 'new1' }, { kind: 'image', id: 'new2' }] })
    emit('step_finished', 3, { step_id: 's1', ok: true, preview: 'Created 2 image(s)' })
    emit('turn_done', 4)
    const run = screen.getByRole('region', { name: 'New images' })
    expect(within(run).getByRole('img', { name: 'Generated image 1' })).toHaveAttribute('src', '/api/images/new1/file')
    const gallery = screen.getByRole('region', { name: 'Your images' })
    const tiles = within(gallery).getAllByRole('img')
    expect(tiles.map((t) => t.getAttribute('src'))).toEqual(['/api/images/new1/file', '/api/images/new2/file'])
    expect(calls.filter((c) => c.path === PAGE)).toHaveLength(1)
  })

  it('images view creates the Images chat once and reuses it', async () => {
    const { calls } = routes()
    render(<Images />)
    await describeAndCreate('A cat')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(localStorage.getItem(IMAGES_CHAT_KEY)).toBe('ic1')
    emit('turn_done', 1)

    await describeAndCreate('A dog')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2))
    expect(calls.filter((c) => c.path === '/api/chats' && c.init.method === 'POST')).toHaveLength(1)
    const messages = calls.filter((c) => c.path === '/api/chats/ic1/messages')
    expect(messages.map((c) => bodyOf(c).text)).toEqual([
      'Create 4 image(s), shape square: A cat',
      'Create 4 image(s), shape square: A dog',
    ])
  })

  it('recreates the Images chat when the remembered one is gone', async () => {
    localStorage.setItem(IMAGES_CHAT_KEY, 'old')
    const { calls } = routes({ 'POST /api/chats/old/messages': { status: 404, body: { reason: 'not_found' } } })
    render(<Images />)
    await describeAndCreate('A cat')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(calls.filter((c) => c.path === '/api/chats' && c.init.method === 'POST')).toHaveLength(1)
    expect(localStorage.getItem(IMAGES_CHAT_KEY)).toBe('ic1')
    expect(lastSource().url).toBe('/api/chats/ic1/turns/t1/events?after=0')
  })

  it('works when the browser keeps no storage', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const { calls } = routes()
    render(<Images />)
    await describeAndCreate('A cat')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    emit('turn_done', 1)
    await describeAndCreate('A dog')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2))
    expect(calls.filter((c) => c.path === '/api/chats' && c.init.method === 'POST')).toHaveLength(1)
    vi.restoreAllMocks()
  })

  it('gallery paginates with Load more', async () => {
    const first = Array.from({ length: 24 }, (_, i) => image(i + 1))
    const before = encodeURIComponent(first[23].created_at)
    const { calls } = routes({
      [`GET ${PAGE}`]: { body: { images: first } },
      [`GET ${PAGE}&before=${before}`]: { body: { images: [image(25), image(26)] } },
    })
    render(<Images />)
    const gallery = await screen.findByRole('region', { name: 'Your images' })
    await waitFor(() => expect(within(gallery).getAllByRole('img')).toHaveLength(24))
    expect(within(gallery).getAllByRole('img')[0]).toHaveAttribute('src', '/api/images/img1/file')

    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(within(gallery).getAllByRole('img')).toHaveLength(26))
    expect(calls.map((c) => c.path)).toContain(`${PAGE}&before=${before}`)
    // A short page is the last one.
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('shows the empty state when there are no images yet', async () => {
    routes()
    render(<Images />)
    expect(await screen.findByText('Your images will appear here. Describe one above to start.')).toBeInTheDocument()
  })

  it('shows not-set-up state when there are no image models', async () => {
    routes({
      'GET /api/models': {
        body: {
          models: [models.body.models[0]],
          errors: [{ id_or_file: 'zimage.toml', message: 'missing workflow' }],
          gpu: { resident: [] },
        },
      },
    })
    render(<Images />)
    expect(await screen.findByText("Image generation isn't set up on this PC yet.")).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /set up image models/i })).toHaveAttribute('href')
    expect(screen.queryByRole('button', { name: 'Create' })).toBeNull()
    expect(screen.queryByText('Your images will appear here. Describe one above to start.')).toBeNull()
  })

  it('copy prompt copies the prompt text', async () => {
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    routes({ [`GET ${PAGE}`]: { body: { images: [image(1, { width: 1536, height: 864 })] } } })
    render(<Images />)
    const gallery = await screen.findByRole('region', { name: 'Your images' })
    const tile = await within(gallery).findByRole('button', { name: 'A prompt for image 1' })
    fireEvent.click(tile)

    const viewer = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    expect(within(viewer).getByText('A prompt for image 1')).toBeInTheDocument()
    expect(within(viewer).getByText('Z-Image Turbo')).toBeInTheDocument()
    expect(within(viewer).getByText('1536 × 864')).toBeInTheDocument()
    await act(async () => fireEvent.click(within(viewer).getByRole('button', { name: 'Copy prompt' })))
    expect(writeText).toHaveBeenCalledWith('A prompt for image 1')
    expect(within(viewer).getByRole('status')).toHaveTextContent('Copied')
    delete (navigator as { clipboard?: unknown }).clipboard
  })
})
