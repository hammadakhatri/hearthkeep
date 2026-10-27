import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Chat } from '../views/Chat'
import { IMAGES_CHAT_KEY, Images, composeMessage, nearestShape } from '../views/Images'
import { bodyOf, headerOf, mockFetch, type Reply } from '../test/fetch'
import { emit, FakeEventSource, installEventSource, lastSource } from '../test/eventsource'

const PAGE = '/api/images?limit=24'

const models = {
  body: {
    models: [
      { id: 'gemma', name: 'Local model 12B', kind: 'chat', capabilities: [], vram_gb: 9, source: 'card', resident: true, managed: true },
      { id: 'zimage', name: 'Z-Image Turbo', kind: 'image', capabilities: ['text'], vram_gb: 12, source: 'card', resident: false, managed: false },
      { id: 'flux', name: 'FLUX klein', kind: 'image', capabilities: ['photoreal', 'image-edit'], vram_gb: 10, source: 'card', resident: false, managed: false },
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
    const run = screen.getByRole('region', { name: 'New images' })
    expect(within(run).getByRole('img', { name: 'Generated image 1' })).toHaveAttribute('src', '/api/images/new1/file')
    // Prepended while the turn runs, with no refetch...
    const gallery = screen.getByRole('region', { name: 'Your images' })
    const tiles = within(gallery).getAllByRole('img')
    expect(tiles.map((t) => t.getAttribute('src'))).toEqual(['/api/images/new1/file', '/api/images/new2/file'])
    expect(calls.filter((c) => c.path === PAGE)).toHaveLength(1)
    // ...and page 1 is fetched again once it is done, for the full records.
    emit('turn_done', 4)
    await waitFor(() => expect(calls.filter((c) => c.path === PAGE)).toHaveLength(2))
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
    expect(screen.getByText('See docs/models.md in the Hearthkeep repository.')).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Create' })).toBeNull()
    expect(screen.queryByText('Your images will appear here. Describe one above to start.')).toBeNull()
  })

  it('after the turn the viewer shows the prompt and Copy prompt', async () => {
    let pages = 0
    routes({
      [`GET ${PAGE}`]: () => {
        pages += 1
        return { body: { images: pages === 1 ? [] : [image(1, { id: 'new1', prompt: 'The prompt the model wrote' })] } }
      },
    })
    render(<Images />)
    await screen.findByRole('option', { name: 'Z-Image Turbo' })
    fireEvent.change(screen.getByRole('combobox', { name: 'Model' }), { target: { value: 'zimage' } })
    fireEvent.click(within(screen.getByRole('group', { name: 'Shape' })).getByRole('button', { name: 'Landscape' }))
    await describeAndCreate('A lighthouse at dusk')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    emit('step_started', 1, { step_id: 's1', tool: 'generate_image', summary: 'Create 4 image(s)' })
    emit('attachments', 2, { step_id: 's1', items: [{ kind: 'image', id: 'new1' }] })
    emit('step_finished', 3, { step_id: 's1', ok: true, preview: 'ok' })
    emit('turn_done', 4)

    // The run panel's viewer knows what was asked, with which model and at what size.
    const run = screen.getByRole('region', { name: 'New images' })
    fireEvent.click(within(run).getByRole('button', { name: 'Generated image 1' }))
    let viewer = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    expect(within(viewer).getByText('A lighthouse at dusk')).toBeInTheDocument()
    expect(within(viewer).getByText('Z-Image Turbo')).toBeInTheDocument()
    expect(within(viewer).getByText('1536 × 864')).toBeInTheDocument()
    expect(within(viewer).getByRole('button', { name: 'Copy prompt' })).toBeInTheDocument()
    fireEvent.keyDown(viewer, { key: 'Escape' })

    // The gallery refetches its first page once the turn is done, so the server's record wins.
    const gallery = screen.getByRole('region', { name: 'Your images' })
    const tile = await within(gallery).findByRole('button', { name: 'The prompt the model wrote' })
    expect(pages).toBe(2)
    fireEvent.click(tile)
    viewer = screen.getByRole('dialog', { name: 'Image 1 of 1' })
    expect(within(viewer).getByText('The prompt the model wrote')).toBeInTheDocument()
    expect(within(viewer).getByRole('button', { name: 'Copy prompt' })).toBeInTheDocument()
  })

  it('keeps the open gallery image when a new one arrives', async () => {
    routes({ [`GET ${PAGE}`]: { body: { images: [image(1), image(2)] } } })
    render(<Images />)
    const gallery = await screen.findByRole('region', { name: 'Your images' })
    await within(gallery).findByRole('button', { name: 'A prompt for image 2' })
    await describeAndCreate('A cat')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    fireEvent.click(within(gallery).getByRole('button', { name: 'A prompt for image 2' }))
    expect(screen.getByRole('dialog', { name: 'Image 2 of 2' })).toBeInTheDocument()

    emit('step_started', 1, { step_id: 's1', tool: 'generate_image', summary: 'Create 4 image(s)' })
    emit('attachments', 2, { step_id: 's1', items: [{ kind: 'image', id: 'new1' }] })
    const viewer = screen.getByRole('dialog', { name: 'Image 3 of 3' })
    expect(within(viewer).getByRole('img', { name: 'A prompt for image 2' })).toHaveAttribute('src', '/api/images/img2/file')
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


// ---- the owner's own photo -------------------------------------------------------------------

const photo = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'me.png', { type: 'image/png' })
const chatC1 = { id: 'c1', title: '', think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }
const uploaded = (id: string, width = 1200, height = 1200) => ({ status: 201, body: { id, width, height } })

function fileInput(): HTMLInputElement {
  return document.querySelector<HTMLInputElement>('input[type="file"]')!
}

function pickPhoto(file: File = photo) {
  fireEvent.change(fileInput(), { target: { files: [file] } })
}

/** The always-present live region that announces the photo. */
function announcer(): HTMLElement {
  return screen.getByTestId('photo-status')
}

async function photoReady() {
  await waitFor(() => expect(announcer()).toHaveTextContent('Photo attached'))
}

function chip(): HTMLElement | null {
  return document.querySelector('.photo-chip')
}

function userMessage(id: number, content: string) {
  return { id, role: 'user', content, tool_calls: null, tool_call_id: null, created_at: '2026-10-26T10:00:00Z' }
}

describe('photos', () => {
  it('chat attaches a photo and shows it in the bubble', async () => {
    const { calls } = mockFetch({
      'GET /api/chats': { body: [] },
      'POST /api/chats': { status: 201, body: chatC1 },
      'POST /api/uploads': uploaded('up1'),
      'POST /api/chats/c1/messages': { status: 202, body: { turn_id: 't1' } },
    })
    render(<Chat model="Local model 12B" />)
    const clip = await screen.findByRole('button', { name: 'Attach a photo or file' })
    expect(clip).toBeEnabled()
    // One paperclip for photos and files; a photo is still routed down the photo path.
    expect(fileInput().accept.split(',')).toEqual(
      expect.arrayContaining(['image/png', 'image/jpeg', 'image/webp', '.csv']),
    )
    expect(fileInput().multiple).toBe(true)
    // The live region is there before anything happens, so screen readers pick up changes.
    expect(announcer()).toHaveAttribute('role', 'status')
    expect(announcer()).toHaveTextContent('')

    // Uploaded as soon as it is picked, then shown as a pending chip and announced.
    pickPhoto()
    await photoReady()
    expect(chip()).not.toHaveAttribute('role')
    expect(within(chip()!).getByRole('img', { name: 'Your photo' })).toHaveAttribute('src', '/api/uploads/up1/file')
    const upload = calls.find((c) => c.path === '/api/uploads')!
    expect(upload.init.method).toBe('POST')
    expect(headerOf(upload, 'X-Hearthkeep')).toBe('1')
    expect(upload.init.body).toBeInstanceOf(FormData)
    expect((upload.init.body as FormData).get('file')).toBe(photo)

    fireEvent.change(screen.getByRole('textbox', { name: 'Message Hearthkeep' }), {
      target: { value: 'Make me look like a painting' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/c1/messages')!
    expect(bodyOf(message)).toEqual({ text: 'Make me look like a painting\nAttached photo: up1' })

    // The bubble shows the photo and the words, never the attachment line itself.
    const bubble = document.querySelector('.me') as HTMLElement
    expect(bubble).toHaveTextContent('Make me look like a painting')
    expect(bubble).not.toHaveTextContent('Attached photo')
    expect(within(bubble).getByRole('img', { name: 'Your photo' })).toHaveAttribute('src', '/api/uploads/up1/file')
    // The optimistic title is the words only.
    expect(document.querySelector('.chat-bar-title')).toHaveTextContent(/^Make me look like a painting$/)
    // One photo per message: the chip is gone once it is sent.
    expect(chip()).toBeNull()
  })

  it('chat sends a photo with no words, and Remove drops it', async () => {
    const { calls } = mockFetch({
      'GET /api/chats': { body: [] },
      'POST /api/chats': { status: 201, body: chatC1 },
      'POST /api/uploads': uploaded('up2'),
      'POST /api/chats/c1/messages': { status: 202, body: { turn_id: 't1' } },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    pickPhoto()
    await photoReady()
    fireEvent.click(screen.getByRole('button', { name: 'Remove photo' }))
    expect(chip()).toBeNull()
    expect(announcer()).toHaveTextContent('Photo removed')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()

    pickPhoto()
    await photoReady()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/c1/messages')!
    expect(bodyOf(message)).toEqual({ text: 'Attached photo: up2' })
    // A photo on its own is titled "Photo", not with its attachment line.
    expect(document.querySelector('.chat-bar-title')).toHaveTextContent(/^Photo$/)
    expect(screen.getByRole('button', { name: /^Photo/ })).toBeInTheDocument()
  })

  it('chat drops the pending photo on New chat and when another chat opens', async () => {
    const other = { ...chatC1, id: 'c2', title: 'Older chat' }
    mockFetch({
      'GET /api/chats': { body: [{ ...chatC1, title: 'Recent chat' }, other] },
      'GET /api/chats/c1/messages': { body: [] },
      'GET /api/chats/c2/messages': { body: [] },
      'POST /api/uploads': uploaded('up4'),
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Older chat' })

    pickPhoto()
    await photoReady()
    fireEvent.click(screen.getByRole('button', { name: 'Older chat' }))
    await waitFor(() => expect(chip()).toBeNull())

    pickPhoto()
    await photoReady()
    fireEvent.click(screen.getAllByRole('button', { name: 'New chat' })[0])
    expect(chip()).toBeNull()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('chat shows the upload error from the server', async () => {
    const message = 'That photo is too big. Photos can be up to 15 MB.'
    mockFetch({
      'GET /api/chats': { body: [] },
      'POST /api/uploads': { status: 413, body: { reason: 'too_large', message } },
    })
    render(<Chat model="Local model 12B" />)
    await screen.findByRole('button', { name: 'Attach a photo or file' })
    pickPhoto()
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(chip()).toBeNull()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('chat bubble hides the attached-photo line and shows the thumbnail when reopened', async () => {
    const saved = { ...chatC1, title: 'Fix this' }
    mockFetch({
      'GET /api/chats': { body: [saved] },
      'GET /api/chats/c1/messages': {
        body: [userMessage(1, 'Fix this\nAttached photo: up9\nAttached photo: not an id!\n Attached photo: up8')],
      },
    })
    render(<Chat model="Local model 12B" />)
    await waitFor(() => expect(document.querySelector('.me')).not.toBeNull())
    const bubble = document.querySelector('.me') as HTMLElement
    const photos = within(bubble).getAllByRole('img', { name: 'Your photo' })
    expect(photos.map((img) => img.getAttribute('src'))).toEqual(['/api/uploads/up9/file'])
    expect(bubble).toHaveTextContent('Fix this')
    expect(bubble).not.toHaveTextContent('Attached photo: up9')
    // Only lines that match exactly are photos; anything else stays as the owner wrote it.
    expect(bubble).toHaveTextContent('Attached photo: not an id!')
    expect(bubble).toHaveTextContent('Attached photo: up8')
  })

  it('images view use my photo selects FLUX and sends the upload id', async () => {
    const { calls } = routes({ 'POST /api/uploads': uploaded('up1') })
    render(<Images />)
    await screen.findByRole('option', { name: 'Z-Image Turbo' })
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('auto')
    const use = screen.getByRole('button', { name: 'Use my photo' })
    expect(use).toBeEnabled()
    expect(fileInput()).toHaveAttribute('accept', 'image/png,image/jpeg,image/webp')

    pickPhoto()
    await photoReady()
    expect(within(chip()!).getByRole('img', { name: 'Your photo' })).toHaveAttribute('src', '/api/uploads/up1/file')
    // The first card that can edit photos, found from /api/models, not a fixed id.
    expect(screen.getByRole('combobox', { name: 'Model' })).toHaveValue('flux')

    await describeAndCreate('Make the sky pink')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/ic1/messages')!
    expect(bodyOf(message)).toEqual({
      text: 'Create 4 image(s), shape square, using FLUX klein: Make the sky pink\nAttached photo: up1',
    })
    expect(chip()).toBeNull()
  })

  it('images view use my photo preselects the nearest shape, which the owner can change', async () => {
    const { calls } = routes({ 'POST /api/uploads': uploaded('up5', 900, 1600) })
    render(<Images />)
    await screen.findByRole('option', { name: 'Z-Image Turbo' })
    const shapes = screen.getByRole('group', { name: 'Shape' })
    expect(within(shapes).getByRole('button', { name: 'Square' })).toHaveAttribute('aria-pressed', 'true')

    pickPhoto()
    await photoReady()
    expect(within(shapes).getByRole('button', { name: 'Portrait' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(shapes).getByRole('button', { name: 'Square' })).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(within(shapes).getByRole('button', { name: 'Landscape' }))
    await describeAndCreate('Make it autumn')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/ic1/messages')!
    expect(bodyOf(message).text).toBe(
      'Create 4 image(s), shape landscape, using FLUX klein: Make it autumn\nAttached photo: up5',
    )
  })

  it('nearestShape picks the shape closest to the photo', () => {
    expect(nearestShape(1600, 900)).toBe('landscape')
    expect(nearestShape(900, 1600)).toBe('portrait')
    expect(nearestShape(1000, 980)).toBe('square')
    expect(nearestShape(3000, 1200)).toBe('banner')
  })

  it('images view use my photo can be removed and shows upload errors', async () => {
    let replies = 0
    const { calls } = routes({
      'POST /api/uploads': () => {
        replies += 1
        return replies === 1
          ? { status: 415, body: { reason: 'animated', message: 'Hearthkeep can only use still photos.' } }
          : uploaded('up3')
      },
    })
    render(<Images />)
    await screen.findByRole('option', { name: 'Z-Image Turbo' })
    pickPhoto()
    expect(await screen.findByRole('alert')).toHaveTextContent('Hearthkeep can only use still photos.')
    pickPhoto()
    await photoReady()
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Remove photo' }))
    expect(announcer()).toHaveTextContent('Photo removed')
    await describeAndCreate('A plain fox')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const message = calls.find((c) => c.path === '/api/chats/ic1/messages')!
    expect(bodyOf(message).text).toBe('Create 4 image(s), shape square, using FLUX klein: A plain fox')
  })

  it('images view disables use my photo when no model can edit photos', async () => {
    routes({
      'GET /api/models': {
        body: { ...models.body, models: models.body.models.map((m) => ({ ...m, capabilities: [] })) },
      },
    })
    render(<Images />)
    await screen.findByRole('option', { name: 'Z-Image Turbo' })
    const use = screen.getByRole('button', { name: 'Use my photo' })
    expect(use).toBeDisabled()
    expect(use).toHaveAccessibleDescription('None of your image models can edit photos.')
  })
})
