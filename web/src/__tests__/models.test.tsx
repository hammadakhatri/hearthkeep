import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Catalog, CatalogItem, Job, JobItem, ModelsResponse } from '../api/models'
import { formatBytes } from '../api/models'
import { stepUp } from '../api/webauthn'
import { AppShell } from '../shell/AppShell'
import { AddonsPanel } from '../components/AddonsPanel'
import { DownloadList, ITEM_ERRORS } from '../components/DownloadList'
import { MODELS_ERRORS, ModelsPanel } from '../components/ModelsPanel'
import { ModelList } from '../components/ModelList'
import { ServerProvider } from '../components/RestartNotice'
import { SetUpPanel } from '../components/SetUpPanel'
import { forbiddenIn } from '../test/copyRules'
import { renderWithAddons } from '../test/addons'
import { addonList, bodyOf, healthy, mockFetch, serverInfo, type Handler, type Reply } from '../test/fetch'
import type { Addon } from '../api/addons'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const base: ModelsResponse = {
  models: [
    {
      id: 'chat-main',
      name: 'Local chat 12B',
      kind: 'chat',
      capabilities: ['tools', 'vision'],
      vram_gb: 9,
      source: 'shipped',
      resident: true,
      managed: true,
    },
    {
      id: 'img-a',
      name: 'Picture maker',
      kind: 'image',
      capabilities: ['text-to-image'],
      vram_gb: 12,
      source: 'owner',
      resident: false,
      managed: true,
    },
  ],
  errors: [],
  gpu: { total_gb: 24, used_gb: 9.25, resident: ['chat-main'], waiting: 0 },
}

const zeroGpu = { ...base.gpu, total_gb: 0, used_gb: 0 }
const DOCS = 'Add models in Settings › Chat model.'

describe('Models list', () => {
  it('lists models with kind and status', async () => {
    mockFetch({ 'GET /api/models': { body: base } })
    render(<ModelList />)
    const chat = (await screen.findByText('Local chat 12B')).closest('li')!
    expect(within(chat).getByText('Chat')).toBeInTheDocument()
    expect(within(chat).getByText('tools')).toBeInTheDocument()
    expect(within(chat).getByText('vision')).toBeInTheDocument()
    expect(within(chat).getByText('Shipped')).toBeInTheDocument()
    const loaded = within(chat).getByText('Loaded')
    expect(loaded.querySelector('.dot')).toHaveAttribute('aria-hidden', 'true')
    const img = screen.getByText('Picture maker').closest('li')!
    expect(within(img).getByText('Images')).toBeInTheDocument()
    expect(within(img).getByText('Yours')).toBeInTheDocument()
    const ready = within(img).getByText('Ready')
    expect(ready.querySelector('.dot')).toHaveAttribute('aria-hidden', 'true')
  })

  it('lists an image card that Hearthkeep does not manage as Not set up', async () => {
    const models = [base.models[0], { ...base.models[1], managed: false }]
    mockFetch({ 'GET /api/models': { body: { ...base, models } } })
    render(<ModelList />)
    const img = (await screen.findByText('Picture maker')).closest('li')!
    expect(within(img).getByText('Not set up')).toBeInTheDocument()
    expect(within(img).queryByText('Ready')).toBeNull()
    // A chat card on an external server is still just ready.
    const chat = screen.getByText('Local chat 12B').closest('li')!
    expect(within(chat).queryByText('Not set up')).toBeNull()
  })

  it('omits the chip list when a model has no capabilities', async () => {
    const models = [{ ...base.models[0], capabilities: [] }]
    mockFetch({ 'GET /api/models': { body: { ...base, models } } })
    render(<ModelList />)
    await screen.findByText('Local chat 12B')
    expect(screen.queryByLabelText(/capabilities/)).toBeNull()
  })

  it('shows card errors', async () => {
    const errors = [{ id_or_file: 'broken.toml', message: 'vram_gb is missing' }]
    mockFetch({ 'GET /api/models': { body: { ...base, errors } } })
    render(<ModelList />)
    const name = await screen.findByText('broken.toml')
    expect(name.tagName).toBe('B')
    const item = name.closest('li')!
    expect(item.parentElement!.tagName).toBe('UL')
    expect(item).toHaveTextContent('broken.toml vram_gb is missing')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows card errors when there are no models', async () => {
    const errors = [{ id_or_file: 'only.toml', message: 'bad card' }]
    mockFetch({ 'GET /api/models': { body: { ...base, models: [], errors } } })
    render(<ModelList />)
    expect(await screen.findByText('only.toml')).toBeInTheDocument()
    expect(screen.getByText(/bad card/)).toBeInTheDocument()
    expect(screen.queryByText('No models yet.')).toBeNull()
  })

  it('shows gpu usage', async () => {
    mockFetch({ 'GET /api/models': { body: { ...base, gpu: { ...base.gpu, waiting: 2 } } } })
    render(<ModelList />)
    expect(await screen.findByText('9.3 of 24.0 GB in use · 2 waiting')).toBeInTheDocument()
    const meter = screen.getByRole('meter')
    expect(meter).toHaveAttribute('aria-valuenow', '9.25')
    expect(meter).toHaveAttribute('aria-valuemin', '0')
    expect(meter).toHaveAttribute('aria-valuemax', '24')
    expect(meter).toHaveAttribute('aria-valuetext', '9.3 of 24.0 GB in use')
  })

  it('does not mention waiting when nothing waits', async () => {
    mockFetch({ 'GET /api/models': { body: base } })
    render(<ModelList />)
    expect(await screen.findByText('9.3 of 24.0 GB in use')).toBeInTheDocument()
    expect(screen.queryByText(/waiting/)).toBeNull()
  })

  it('hides the gpu bar when the total is 0 or missing', async () => {
    mockFetch({ 'GET /api/models': { body: { ...base, gpu: zeroGpu } } })
    const { unmount } = render(<ModelList />)
    await screen.findByText('Local chat 12B')
    expect(screen.queryByRole('meter')).toBeNull()
    expect(screen.queryByText(/in use/)).toBeNull()
    unmount()
    mockFetch({ 'GET /api/models': { body: { ...base, gpu: undefined } } })
    render(<ModelList />)
    await screen.findByText('Local chat 12B')
    expect(screen.queryByRole('meter')).toBeNull()
    expect(screen.queryByText(/in use/)).toBeNull()
  })

  it('says so when there are no models', async () => {
    mockFetch({ 'GET /api/models': { body: { ...base, models: [] } } })
    render(<ModelList />)
    expect(await screen.findByText('No models yet.')).toBeInTheDocument()
    expect(screen.getByText(DOCS)).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it("shows a quiet line with Retry when models can't load", async () => {
    let fail = true
    mockFetch({ 'GET /api/models': () => (fail ? { status: 500, body: {} } : { body: base }) })
    render(<ModelList />)
    expect(await screen.findByText(/Couldn't load models\./)).toBeInTheDocument()
    fail = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Local chat 12B')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load models/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })
})

describe('status pill', () => {
  it('status pill describes gpu use', async () => {
    mockFetch({ 'GET /api/health': healthy, 'GET /api/models': { body: base } })
    render(<AppShell onSignOut={() => {}} />)
    // The chat's photo live region is a status too: pick the pill by its own text.
    const pill = (await screen.findByText('Local model 12B', { selector: '.status-text' })).closest<HTMLElement>(
      '[role="status"]',
    )!
    await waitFor(() => expect(pill).toHaveAttribute('title', expect.stringContaining('GPU 9.3 of 24.0 GB in use')))
    const desc = document.getElementById(pill.getAttribute('aria-describedby')!)!
    expect(desc).toHaveTextContent('GPU 9.3 of 24.0 GB in use')
    // Outside the live region, and the visible text is unchanged.
    expect(pill).not.toContainElement(desc)
    expect(pill).toHaveTextContent(/^Local model 12B$/)
  })

  it('has no double period in the title when the chat model is offline', async () => {
    const offline = { body: { status: 'ok', chat_model: 'Local model 12B', chat_model_reachable: false } }
    mockFetch({ 'GET /api/health': offline, 'GET /api/models': { body: base } })
    render(<AppShell onSignOut={() => {}} />)
    const pill = (await screen.findByText('Chat model offline')).closest('[role="status"]')!
    await waitFor(() => expect(pill).toHaveAttribute('title', expect.stringContaining('GPU')))
    expect(pill.getAttribute('title')).not.toContain('..')
  })

  it('adds no gpu text when the total is 0', async () => {
    mockFetch({ 'GET /api/health': healthy, 'GET /api/models': { body: { ...base, gpu: zeroGpu } } })
    render(<AppShell onSignOut={() => {}} />)
    const pill = (await screen.findByText('Local model 12B', { selector: '.status-text' })).closest('[role="status"]')!
    await waitFor(() => expect(pill).toHaveAttribute('data-state', 'online'))
    expect(screen.queryByText(/GPU/)).toBeNull()
    expect(pill).not.toHaveAttribute('aria-describedby')
    expect(pill.getAttribute('title')).not.toContain('GPU')
  })

  it('a models failure does not change the health state', async () => {
    mockFetch({ 'GET /api/health': healthy, 'GET /api/models': { status: 500, body: {} } })
    render(<AppShell onSignOut={() => {}} />)
    const pill = await screen.findByText('Local model 12B', { selector: '.status-text' })
    expect(pill.closest('[role="status"]')).toHaveAttribute('data-state', 'online')
    expect(pill.closest('[role="status"]')).not.toHaveAttribute('aria-describedby')
  })
})

// ---- Settings › Chat model and Image models ------------------------------------------------

const RTX = { gpu_name: 'NVIDIA GeForce RTX 4080', vram_gb: 16, nvidia: true, ram_gb: 32, free_disk_gb: 400 }
const NO_GPU = { gpu_name: null, vram_gb: null, nvidia: false, ram_gb: 16, free_disk_gb: 400 }

function item(over: Partial<CatalogItem> & Pick<CatalogItem, 'id' | 'kind' | 'name' | 'size_bytes'>): CatalogItem {
  return {
    note: '',
    licence: 'Apache 2.0',
    licence_url: `https://huggingface.co/example/${over.id}`,
    needs_nvidia: false,
    variant: '',
    card: '',
    installed: false,
    fits: true,
    ...over,
  }
}

const ITEMS: CatalogItem[] = [
  item({
    id: 'llama-cuda',
    kind: 'runtime',
    name: 'Chat engine for NVIDIA graphics cards',
    size_bytes: 678_456_271,
    variant: 'cuda',
    needs_nvidia: true,
    licence: 'MIT',
  }),
  item({
    id: 'llama-vulkan',
    kind: 'runtime',
    name: 'Chat engine for other graphics cards',
    size_bytes: 37_660_003,
    variant: 'vulkan',
    licence: 'MIT',
  }),
  item({ id: 'llama-cpu', kind: 'runtime', name: 'Chat engine for the processor only', size_bytes: 24_706_916, variant: 'cpu' }),
  item({
    id: 'chat-qwen3.5-4b',
    kind: 'chat',
    name: 'Qwen3.5 4B',
    card: 'qwen3.5-4b',
    size_bytes: 3_013_027_808,
    note: 'Runs on the processor alone, or on under 6 GB of graphics memory.',
  }),
  item({
    id: 'chat-qwen3.5-9b',
    kind: 'chat',
    name: 'Qwen3.5 9B',
    card: 'qwen3.5-9b',
    size_bytes: 6_169_341_984,
    note: 'Good all-rounder for about 8 GB of graphics memory.',
  }),
  item({
    id: 'chat-gemma-4-12b',
    kind: 'chat',
    name: 'Gemma 4 12B',
    card: 'gemma-4-12b',
    size_bytes: 10_361_358_240,
    note: 'Understands pictures too. Best with 11 GB of graphics memory or more.',
  }),
  item({
    id: 'comfyui',
    kind: 'comfyui',
    name: 'Picture maker (ComfyUI)',
    size_bytes: 1_945_636_677,
    needs_nvidia: true,
    licence: 'GPL-3.0',
  }),
  item({
    id: 'image-z-image-turbo',
    kind: 'image',
    name: 'Z-Image Turbo',
    card: 'z-image-turbo',
    size_bytes: 20_690_152_836,
    needs_nvidia: true,
  }),
  item({
    id: 'image-flux2-klein-4b',
    kind: 'image',
    name: 'FLUX.2 klein 4B',
    card: 'flux2-klein-4b',
    size_bytes: 16_132_299_052,
    needs_nvidia: true,
  }),
]

const GEMMA = ITEMS.find((i) => i.id === 'chat-gemma-4-12b')!

function catalog(over: Partial<Catalog> = {}, installed: string[] = []): Catalog {
  const nvidia = (over.pc ?? RTX).nvidia
  return {
    pc: RTX,
    recommended: { chat: 'chat-gemma-4-12b', runtime: 'llama-cuda', reason: 'fits_gpu' },
    items: ITEMS.map((i) => ({
      ...i,
      installed: installed.includes(i.id),
      fits: i.needs_nvidia ? nvidia : true,
    })),
    chat: { source: 'none', model: null, base_url: null },
    runtime_ready: installed.some((id) => id.startsWith('llama-')),
    restart_needed: false,
    supervised: true,
    ...over,
  }
}

function jobItem(over: Partial<JobItem> & Pick<JobItem, 'id' | 'name'>): JobItem {
  return { state: 'queued', done_bytes: 0, total_bytes: 1_000_000_000, speed_bps: 0, error: null, ...over }
}

function job(items: JobItem[], over: Partial<Job> = {}): Job {
  return { id: 'job-1', state: 'running', items, restart_needed: false, ...over }
}

const noJob: Reply = { body: { job: null } }

function modelRoutes(cat: Catalog, extra: Record<string, Handler | Reply> = {}) {
  return mockFetch({
    'GET /api/models/catalog': { body: cat },
    'GET /api/models/install': noJob,
    'GET /api/server': serverInfo(),
    ...extra,
  })
}

function renderPanel(pollMs = 1000) {
  return render(
    <ServerProvider>
      <ModelsPanel pollMs={pollMs} />
    </ServerProvider>,
  )
}

function chatSection(): HTMLElement {
  return document.getElementById('settings-chat')!
}

function imageSection(): HTMLElement {
  return document.getElementById('settings-images')!
}

async function row(name: string): Promise<HTMLElement> {
  return (await screen.findByText(name, { selector: '.model-name' })).closest('li')!
}

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

describe('Settings › Chat model and Image models', () => {
  beforeEach(() => {
    vi.mocked(stepUp).mockReset().mockResolvedValue('step-token')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('formats sizes in decimal units', () => {
    expect(formatBytes(3_013_027_808)).toBe('3.0 GB')
    expect(formatBytes(6_169_341_984)).toBe('6.2 GB')
    expect(formatBytes(287_012_644)).toBe('287 MB')
    expect(formatBytes(24_706)).toBe('25 KB')
    expect(formatBytes(512)).toBe('512 B')
  })

  it('shows the recommendation and PC line', async () => {
    modelRoutes(catalog())
    renderPanel()
    expect(await screen.findByText('NVIDIA GeForce RTX 4080, 16 GB')).toBeInTheDocument()
    const gemma = await row('Gemma 4 12B')
    expect(within(gemma).getByText('Recommended for this PC')).toBeInTheDocument()
    expect(within(gemma).getByText('10.4 GB')).toBeInTheDocument()
    expect(within(gemma).getByText(GEMMA.note)).toBeInTheDocument()
    expect(within(gemma).getByRole('link', { name: /Apache 2\.0/ })).toHaveAttribute('href', GEMMA.licence_url)
    expect(within(await row('Qwen3.5 9B')).queryByText('Recommended for this PC')).toBeNull()
    expect(within(chatSection()).getByRole('heading', { level: 2, name: 'Chat model' })).toBeInTheDocument()
    expect(within(imageSection()).getByRole('heading', { level: 2, name: 'Image models' })).toBeInTheDocument()
  })

  it('says what is in use', async () => {
    modelRoutes(
      catalog({ chat: { source: 'local', model: 'qwen3.5-4b', base_url: null } }, ['chat-qwen3.5-4b', 'llama-cpu']),
    )
    const { unmount } = renderPanel()
    expect(await within(chatSection()).findByText('In use: Qwen3.5 4B')).toBeInTheDocument()
    expect(within(await row('Qwen3.5 4B')).queryByRole('button', { name: 'Use this: Qwen3.5 4B' })).toBeNull()
    unmount()
    modelRoutes(catalog({ chat: { source: 'external', model: 'local', base_url: 'http://127.0.0.1:8080/v1' } }))
    renderPanel()
    expect(await within(chatSection()).findByText('In use: A model server you run')).toBeInTheDocument()
    expect(within(chatSection()).getByText('http://127.0.0.1:8080/v1')).toBeInTheDocument()
  })

  it('download lists the files and total size, asks for a passkey, then shows progress', async () => {
    const started = job([
      jobItem({ id: 'llama-cuda', name: 'Chat engine for NVIDIA graphics cards', total_bytes: 678_456_271 }),
      jobItem({ id: 'chat-gemma-4-12b', name: 'Gemma 4 12B', total_bytes: 10_361_358_240 }),
    ])
    const { calls } = modelRoutes(catalog(), {
      'POST /api/models/install': { status: 202, body: { job: started } },
    })
    renderPanel(60_000)
    fireEvent.click(within(await row('Gemma 4 12B')).getByRole('button', { name: 'Download Gemma 4 12B' }))

    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    const lines = within(confirm)
      .getAllByRole('listitem')
      .map((li) => li.textContent)
    expect(lines).toEqual([
      expect.stringContaining('Chat engine for NVIDIA graphics cards'),
      expect.stringContaining('Gemma 4 12B'),
    ])
    expect(within(confirm).getByText('Up to 11.0 GB')).toBeInTheDocument()
    expect(within(confirm).getByText('It becomes the chat model in use after a restart.')).toBeInTheDocument()
    expect(confirm).toHaveFocus()
    expect(stepUp).not.toHaveBeenCalled()

    fireEvent.click(within(confirm).getByRole('button', { name: 'Download with passkey' }))
    const list = await screen.findByRole('region', { name: 'Downloads' })
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('download')
    const post = calls.find((c) => c.path === '/api/models/install' && c.init.method === 'POST')!
    expect(bodyOf(post)).toEqual({ items: ['chat-gemma-4-12b'], make_default: true, step_up_token: 'step-token' })
    expect(within(list).getAllByRole('progressbar')).toHaveLength(2)
    expect(within(list).getByText('0 of 2 done')).toBeInTheDocument()
    await waitFor(() => expect(list).toHaveFocus())
    // While it runs, nothing else can be downloaded.
    expect(within(await row('Qwen3.5 9B')).getByRole('button', { name: 'Download Qwen3.5 9B' })).toBeDisabled()
  })

  it('progress bar uses transform and reports percent', () => {
    render(
      <DownloadList
        job={job([
          jobItem({
            id: 'chat-qwen3.5-9b',
            name: 'Qwen3.5 9B',
            state: 'downloading',
            done_bytes: 500,
            total_bytes: 1000,
            speed_bps: 25_000_000,
          }),
        ])}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    )
    const bar = screen.getByRole('progressbar', { name: 'Qwen3.5 9B' })
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    const fill = bar.querySelector<HTMLElement>('.download-fill')!
    expect(fill.style.transform).toContain('scaleX(0.5)')
    expect(fill.style.width).toBe('')
    expect(screen.getByText('25 MB/s')).toBeInTheDocument()
  })

  it('shows Verified once checked and the summary counts done items', () => {
    render(
      <DownloadList
        job={job([
          jobItem({ id: 'a', name: 'Alpha', state: 'done', done_bytes: 10, total_bytes: 10 }),
          jobItem({ id: 'b', name: 'Beta', state: 'done', done_bytes: 10, total_bytes: 10 }),
          jobItem({ id: 'c', name: 'Gamma', state: 'downloading', done_bytes: 1, total_bytes: 10 }),
        ])}
        onRetry={() => {}}
        onCancel={() => {}}
      />,
    )
    const summary = screen.getByText('2 of 3 done')
    expect(summary).toHaveAttribute('aria-live', 'polite')
    expect(screen.getAllByText('Verified')).toHaveLength(2)
  })

  it('the summary counts failures, and one Retry serves the whole job', () => {
    const onRetry = vi.fn()
    render(
      <DownloadList
        job={job(
          [
            jobItem({ id: 'a', name: 'Alpha', state: 'done' }),
            jobItem({ id: 'b', name: 'Beta', state: 'failed', error: 'network' }),
            jobItem({ id: 'c', name: 'Gamma', state: 'failed', error: 'checksum' }),
          ],
          { state: 'failed' },
        )}
        onRetry={onRetry}
        onCancel={() => {}}
      />,
    )
    expect(screen.getByText('1 of 3 done, 2 failed')).toHaveAttribute('aria-live', 'polite')
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('failed item shows its sentence and Retry calls retry', async () => {
    const failed = job(
      [
        jobItem({ id: 'llama-cuda', name: 'Chat engine for NVIDIA graphics cards', state: 'done' }),
        jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B', state: 'failed', error: 'checksum' }),
      ],
      { state: 'failed' },
    )
    const again = job([
      jobItem({ id: 'llama-cuda', name: 'Chat engine for NVIDIA graphics cards', state: 'done' }),
      jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B', state: 'queued' }),
    ])
    const { calls } = modelRoutes(catalog(), {
      'GET /api/models/install': { body: { job: failed } },
      'POST /api/models/install/retry': { status: 202, body: { job: again } },
    })
    renderPanel(60_000)
    const list = await screen.findByRole('region', { name: 'Downloads' })
    expect(
      within(list).getByText("The file didn't match its checksum, so it was removed. Retry to download it again."),
    ).toBeInTheDocument()
    expect(within(list).getAllByRole('button', { name: 'Retry' })).toHaveLength(1)
    fireEvent.click(within(list).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls.some((c) => c.path === '/api/models/install/retry')).toBe(true))
    expect(stepUp).not.toHaveBeenCalled()
    await waitFor(() => expect(within(list).queryByRole('button', { name: 'Retry' })).toBeNull())
    expect(list).toHaveFocus()
  })

  it('a stopped item says Stopped and offers Retry', () => {
    const onRetry = vi.fn()
    render(
      <DownloadList
        job={job([jobItem({ id: 'a', name: 'Alpha', state: 'cancelled', error: 'cancelled' })], { state: 'cancelled' })}
        onRetry={onRetry}
        onCancel={() => {}}
      />,
    )
    expect(screen.getByText('Stopped')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('a running job can be stopped', () => {
    const onCancel = vi.fn()
    render(
      <DownloadList
        job={job([jobItem({ id: 'a', name: 'Alpha', state: 'downloading' })])}
        onRetry={() => {}}
        onCancel={onCancel}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Stop downloading' }))
    expect(onCancel).toHaveBeenCalledOnce()
  })

  it('no_space shows needed and free space', async () => {
    modelRoutes(catalog(), {
      'POST /api/models/install': {
        status: 409,
        body: { reason: 'no_space', needed_bytes: 12_300_000_000, free_bytes: 4_100_000_000 },
      },
    })
    renderPanel()
    fireEvent.click(within(await row('Qwen3.5 9B')).getByRole('button', { name: 'Download Qwen3.5 9B' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Download with passkey' }))
    expect(await within(chatSection()).findByRole('alert')).toHaveTextContent(
      "There isn't enough free space: about 12.3 GB is needed and 4.1 GB is free.",
    )
  })

  it('doesnt_fit has its sentence', async () => {
    modelRoutes(catalog(), { 'POST /api/models/install': { status: 400, body: { reason: 'doesnt_fit' } } })
    renderPanel()
    fireEvent.click(within(await row('Qwen3.5 9B')).getByRole('button', { name: 'Download Qwen3.5 9B' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Download with passkey' }))
    expect(await within(chatSection()).findByRole('alert')).toHaveTextContent(
      'This model needs more graphics memory than this PC has.',
    )
  })

  it('busy shows the running job', async () => {
    const running = job([jobItem({ id: 'chat-qwen3.5-4b', name: 'Qwen3.5 4B', state: 'downloading' })])
    modelRoutes(catalog(), {
      'POST /api/models/install': { status: 409, body: { reason: 'busy', job: running } },
    })
    renderPanel(60_000)
    fireEvent.click(within(await row('Qwen3.5 9B')).getByRole('button', { name: 'Download Qwen3.5 9B' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Download with passkey' }))
    expect(await within(chatSection()).findByRole('alert')).toHaveTextContent('A download is already running.')
    expect(await screen.findByRole('region', { name: 'Downloads' })).toBeInTheDocument()
  })

  it('a chat model that does not fit cannot be downloaded', async () => {
    const cat = catalog()
    cat.items = cat.items.map((i) => (i.id === 'chat-gemma-4-12b' ? { ...i, fits: false } : i))
    modelRoutes(cat)
    renderPanel()
    const gemma = await row('Gemma 4 12B')
    expect(within(gemma).getByRole('button', { name: 'Download Gemma 4 12B' })).toBeDisabled()
    expect(within(gemma).getByText('This model needs more graphics memory than this PC has.')).toBeInTheDocument()
  })

  it('switching to an installed model shows the restart notice', async () => {
    const { calls } = modelRoutes(catalog({}, ['chat-qwen3.5-4b', 'llama-cpu']), {
      'POST /api/models/chat': {
        body: { source: 'local', model: 'qwen3.5-4b', base_url: null, restart_needed: true },
      },
    })
    renderPanel()
    const qwen = await row('Qwen3.5 4B')
    expect(within(qwen).getByText('Installed')).toBeInTheDocument()
    expect(within(qwen).queryByRole('button', { name: /^Download/ })).toBeNull()
    fireEvent.click(within(qwen).getByRole('button', { name: 'Use this: Qwen3.5 4B' }))
    expect(await within(chatSection()).findByText('A change needs Hearthkeep to restart.')).toBeInTheDocument()
    expect(within(chatSection()).getByRole('button', { name: 'Restart now' })).toBeInTheDocument()
    const chat = calls.find((c) => c.path === '/api/models/chat')!
    expect(bodyOf(chat)).toEqual({ source: 'local', card: 'qwen3.5-4b' })
    expect(stepUp).not.toHaveBeenCalled()
    await waitFor(() => expect(within(chatSection()).getByText('In use: Qwen3.5 4B')).toHaveFocus())
  })

  it('unsupervised, a switch says to restart Hearthkeep', async () => {
    modelRoutes(catalog({ supervised: false }, ['chat-qwen3.5-4b', 'llama-cpu']), {
      'GET /api/server': serverInfo({ supervised: false }),
      'POST /api/models/chat': {
        body: { source: 'local', model: 'qwen3.5-4b', base_url: null, restart_needed: false },
      },
    })
    renderPanel()
    fireEvent.click(within(await row('Qwen3.5 4B')).getByRole('button', { name: 'Use this: Qwen3.5 4B' }))
    expect(await within(chatSection()).findByText('Restart Hearthkeep to use it.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restart now' })).toBeNull()
  })

  it('a finished job reloads the catalog and asks for a restart', async () => {
    const running = job([jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B', state: 'downloading' })])
    const done = job([jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B', state: 'done' })], {
      state: 'done',
      restart_needed: true,
    })
    let finished = false
    let catalogs = 0
    modelRoutes(catalog(), {
      'GET /api/models/catalog': () => {
        catalogs++
        return { body: finished ? catalog({}, ['chat-qwen3.5-9b', 'llama-cuda']) : catalog() }
      },
      'GET /api/models/install': () => ({ body: { job: finished ? done : running } }),
    })
    renderPanel(20)
    await screen.findByRole('region', { name: 'Downloads' })
    finished = true
    expect(await within(chatSection()).findByText('A change needs Hearthkeep to restart.')).toBeInTheDocument()
    await waitFor(() => expect(catalogs).toBe(2))
    expect(within(await row('Qwen3.5 9B')).getByText('Installed')).toBeInTheDocument()
  })

  it('polls while a job runs, pauses while hidden and stops when it ends', async () => {
    let polls = 0
    let state: Job['state'] = 'running'
    modelRoutes(catalog(), {
      'GET /api/models/install': () => {
        polls++
        const itemState = state === 'running' ? 'downloading' : 'done'
        return { body: { job: job([jobItem({ id: 'a', name: 'Alpha', state: itemState })], { state }) } }
      },
    })
    renderPanel(20)
    await waitFor(() => expect(polls).toBeGreaterThan(3))

    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await pause(60)
    const hiddenAt = polls
    await pause(100)
    expect(polls).toBe(hiddenAt)
    visibility.mockReturnValue('visible')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(polls).toBeGreaterThan(hiddenAt))

    state = 'done'
    expect(await screen.findByText('1 of 1 done')).toBeInTheDocument()
    await pause(60)
    const endedAt = polls
    await pause(100)
    expect(polls).toBe(endedAt)
  })

  it('stops polling on unmount', async () => {
    let polls = 0
    modelRoutes(catalog(), {
      'GET /api/models/install': () => {
        polls++
        return { body: { job: job([jobItem({ id: 'a', name: 'Alpha', state: 'downloading' })]) } }
      },
    })
    const { unmount } = renderPanel(20)
    await waitFor(() => expect(polls).toBeGreaterThan(2))
    unmount()
    const at = polls
    await pause(100)
    expect(polls).toBeLessThanOrEqual(at + 1)
  })

  it('external server form never shows the key back', async () => {
    const { calls } = modelRoutes(catalog(), {
      'POST /api/models/chat': {
        body: { source: 'external', model: 'local', base_url: 'http://127.0.0.1:8080/v1', restart_needed: true },
      },
    })
    const { container } = renderPanel()
    const form = await screen.findByRole('form', { name: 'Use a model server I already run' })
    const address = within(form).getByLabelText('Server address')
    const key = within(form).getByLabelText('Key (optional)')
    expect(key).toHaveAttribute('type', 'password')
    fireEvent.change(address, { target: { value: 'http://127.0.0.1:8080/v1' } })
    fireEvent.change(key, { target: { value: 'sk-very-secret' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Use this server' }))
    expect(await within(chatSection()).findByText('In use: A model server you run')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('settings')
    const post = calls.find((c) => c.path === '/api/models/chat')!
    expect(bodyOf(post)).toEqual({
      source: 'external',
      base_url: 'http://127.0.0.1:8080/v1',
      api_key: 'sk-very-secret',
      model: null,
      step_up_token: 'step-token',
    })
    expect(key).toHaveValue('')
    expect(container.textContent).not.toContain('sk-very-secret')
    for (const input of container.querySelectorAll('input')) expect(input.value).not.toContain('sk-very-secret')
  })

  it('an empty key keeps the stored one, and a bad address has its sentence', async () => {
    const { calls } = modelRoutes(catalog(), {
      'POST /api/models/chat': { status: 400, body: { reason: 'bad_url' } },
    })
    renderPanel()
    const form = await screen.findByRole('form', { name: 'Use a model server I already run' })
    fireEvent.change(within(form).getByLabelText('Server address'), { target: { value: 'http://example.com/v1' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Use this server' }))
    const alert = await within(chatSection()).findByRole('alert')
    expect(alert).toHaveTextContent(MODELS_ERRORS.bad_url)
    const post = calls.find((c) => c.path === '/api/models/chat')!
    expect(bodyOf(post).api_key).toBeNull()
  })

  it('image models are disabled without an NVIDIA GPU', async () => {
    modelRoutes(
      catalog({ pc: NO_GPU, recommended: { chat: 'chat-qwen3.5-4b', runtime: 'llama-cpu', reason: 'no_nvidia' } }),
    )
    renderPanel()
    expect(
      await screen.findByText('No NVIDIA graphics card found: models run on the processor, more slowly'),
    ).toBeInTheDocument()
    const images = imageSection()
    for (const name of ['Picture maker (ComfyUI)', 'Z-Image Turbo', 'FLUX.2 klein 4B']) {
      const r = await row(name)
      expect(images).toContainElement(r)
      expect(within(r).getByRole('button', { name: `Download ${name}` })).toBeDisabled()
    }
    expect(within(images).getAllByText('Needs an NVIDIA graphics card').length).toBeGreaterThan(0)
    expect(within(await row('Z-Image Turbo')).getByText('20.7 GB')).toBeInTheDocument()
  })

  it('an image model download adds ComfyUI when it is missing', async () => {
    modelRoutes(catalog())
    renderPanel()
    fireEvent.click(within(await row('FLUX.2 klein 4B')).getByRole('button', { name: 'Download FLUX.2 klein 4B' }))
    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    expect(imageSection()).toContainElement(confirm)
    const lines = within(confirm)
      .getAllByRole('listitem')
      .map((li) => li.textContent)
    expect(lines).toEqual([
      expect.stringContaining('Picture maker (ComfyUI)'),
      expect.stringContaining('FLUX.2 klein 4B'),
    ])
    expect(within(confirm).getByText('Up to 18.1 GB')).toBeInTheDocument()
    expect(within(confirm).queryByText(/chat model in use/)).toBeNull()
  })

  it('says so when downloads are not available', async () => {
    modelRoutes(catalog(), { 'GET /api/models/catalog': { status: 503, body: { reason: 'unavailable' } } })
    renderPanel()
    expect(await within(chatSection()).findByText(MODELS_ERRORS.unavailable)).toBeInTheDocument()
  })

  it('Set up on the chat card opens settings-chat', async () => {
    const list = addonList({ chat: 'not_set_up', images: 'not_set_up' }) as Addon[]
    const chat = renderWithAddons(<SetUpPanel addon={list[0]} />, list)
    fireEvent.click(within(chat.container).getByRole('button', { name: 'Set up Chat model' }))
    expect(chat.openSettings).toHaveBeenCalledWith('settings-chat')
    const images = renderWithAddons(<SetUpPanel addon={list[2]} />, list)
    fireEvent.click(within(images.container).getByRole('button', { name: 'Set up Images' }))
    expect(images.openSettings).toHaveBeenCalledWith('settings-images')
  })

  it('the add-on cards lead to Settings › Chat model and Image models', async () => {
    mockFetch({ 'GET /api/server': serverInfo() })
    const list = addonList({ chat: 'not_set_up', images: 'not_set_up' }) as Addon[]
    const { openSettings } = renderWithAddons(<AddonsPanel />, list)
    fireEvent.click(await screen.findByRole('button', { name: 'Choose a chat model' }))
    expect(openSettings).toHaveBeenLastCalledWith('settings-chat')
    fireEvent.click(screen.getByRole('button', { name: 'Add image models' }))
    expect(openSettings).toHaveBeenLastCalledWith('settings-images')
  })

  it('a job the server forgot stops polling and reloads the catalog', async () => {
    let forgotten = false
    let polls = 0
    let catalogs = 0
    const downloading = job([jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B', state: 'downloading' })])
    modelRoutes(catalog(), {
      'GET /api/models/catalog': () => {
        catalogs++
        return { body: catalog() }
      },
      'GET /api/models/install': () => {
        polls++
        return { body: { job: forgotten ? null : downloading } }
      },
    })
    renderPanel(20)
    await screen.findByRole('region', { name: 'Downloads' })
    forgotten = true
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Downloads' })).toBeNull())
    await waitFor(() => expect(catalogs).toBe(2))
    const at = polls
    await pause(100)
    expect(polls).toBe(at)
    expect(within(await row('Qwen3.5 9B')).getByRole('button', { name: 'Download Qwen3.5 9B' })).toBeEnabled()
    // One line says what happened and that nothing is lost.
    expect(within(chatSection()).getByText(MODELS_ERRORS.forgotten)).toBeInTheDocument()
    expect(MODELS_ERRORS.forgotten).toBe(
      'The download stopped when Hearthkeep restarted. Download again to continue; nothing that arrived is lost.',
    )
  })

  it('no note about a restart when the page opens with no download', async () => {
    modelRoutes(catalog())
    renderPanel(20)
    await row('Qwen3.5 9B')
    await pause(60)
    expect(screen.queryByText(MODELS_ERRORS.forgotten)).toBeNull()
  })

  it('a retry of a job the server forgot says so in a sentence', async () => {
    let catalogs = 0
    const failed = job([jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B', state: 'failed', error: 'network' })], {
      state: 'failed',
    })
    modelRoutes(catalog(), {
      'GET /api/models/catalog': () => {
        catalogs++
        return { body: catalog() }
      },
      'GET /api/models/install': { body: { job: failed } },
      'POST /api/models/install/retry': { status: 404, body: { reason: 'no_job' } },
    })
    renderPanel(60_000)
    const list = await screen.findByRole('region', { name: 'Downloads' })
    fireEvent.click(within(list).getByRole('button', { name: 'Retry' }))
    const alert = await within(chatSection()).findByRole('alert')
    expect(alert).toHaveTextContent(MODELS_ERRORS.no_job)
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Downloads' })).toBeNull())
    await waitFor(() => expect(catalogs).toBe(2))
    expect(forbiddenIn(MODELS_ERRORS.no_job)).toEqual([])
  })

  it('lists the chat engines, with the recommended one marked', async () => {
    modelRoutes(catalog({}, ['llama-cpu']))
    renderPanel()
    const engines = await within(chatSection()).findByRole('list', { name: 'Chat engines' })
    const names = within(engines)
      .getAllByRole('listitem')
      .map((li) => li.querySelector('.model-name')!.textContent)
    expect(names).toEqual([
      'Chat engine for NVIDIA graphics cards',
      'Chat engine for other graphics cards',
      'Chat engine for the processor only',
    ])
    const cuda = await row('Chat engine for NVIDIA graphics cards')
    expect(within(cuda).getByText('Recommended for this PC')).toBeInTheDocument()
    expect(within(engines).getAllByText('Recommended for this PC')).toHaveLength(1)
    expect(within(await row('Chat engine for the processor only')).getByText('Installed')).toBeInTheDocument()
    // The chat model badge stays on the chat model.
    expect(within(await row('Gemma 4 12B')).getByText('Recommended for this PC')).toBeInTheDocument()
  })

  it('choosing Vulkan posts llama-vulkan only', async () => {
    const started = job([jobItem({ id: 'llama-vulkan', name: 'Chat engine for other graphics cards' })])
    const { calls } = modelRoutes(catalog(), {
      'POST /api/models/install': { status: 202, body: { job: started } },
    })
    renderPanel(60_000)
    const vulkan = await row('Chat engine for other graphics cards')
    fireEvent.click(within(vulkan).getByRole('button', { name: 'Download Chat engine for other graphics cards' }))
    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    expect(within(confirm).getAllByRole('listitem')).toHaveLength(1)
    expect(within(confirm).getByText('Up to 38 MB')).toBeInTheDocument()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Download with passkey' }))
    await screen.findByRole('region', { name: 'Downloads' })
    expect(stepUp).toHaveBeenCalledExactlyOnceWith('download')
    const post = calls.find((c) => c.path === '/api/models/install' && c.init.method === 'POST')!
    expect(bodyOf(post).items).toEqual(['llama-vulkan'])
  })

  it('the CUDA engine needs an NVIDIA graphics card', async () => {
    modelRoutes(
      catalog({ pc: NO_GPU, recommended: { chat: 'chat-qwen3.5-4b', runtime: 'llama-cpu', reason: 'no_nvidia' } }),
    )
    renderPanel()
    const cuda = await row('Chat engine for NVIDIA graphics cards')
    expect(within(cuda).getByRole('button', { name: 'Download Chat engine for NVIDIA graphics cards' })).toBeDisabled()
    expect(within(cuda).getByText('Needs an NVIDIA graphics card')).toBeInTheDocument()
    const cpu = await row('Chat engine for the processor only')
    expect(within(cpu).getByText('Recommended for this PC')).toBeInTheDocument()
  })

  it('an engine the server already has is not listed, even one set up by hand', async () => {
    modelRoutes(catalog({ runtime_ready: true }))
    renderPanel()
    fireEvent.click(within(await row('Gemma 4 12B')).getByRole('button', { name: 'Download Gemma 4 12B' }))
    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    expect(within(confirm).getAllByRole('listitem')).toHaveLength(1)
    expect(within(confirm).getByText('Up to 10.4 GB')).toBeInTheDocument()
  })

  it('with a model server in use, a download does not take over chat', async () => {
    const started = job([jobItem({ id: 'chat-qwen3.5-9b', name: 'Qwen3.5 9B' })])
    const { calls } = modelRoutes(
      catalog({ chat: { source: 'external', model: 'local', base_url: 'http://127.0.0.1:8080/v1' } }),
      { 'POST /api/models/install': { status: 202, body: { job: started } } },
    )
    renderPanel(60_000)
    fireEvent.click(within(await row('Qwen3.5 9B')).getByRole('button', { name: 'Download Qwen3.5 9B' }))
    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    expect(within(confirm).queryByText(/chat model in use/)).toBeNull()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Download with passkey' }))
    await screen.findByRole('region', { name: 'Downloads' })
    const post = calls.find((c) => c.path === '/api/models/install' && c.init.method === 'POST')!
    expect(bodyOf(post).make_default).toBe(false)
  })

  it('without the server state, a switch says to restart Hearthkeep', async () => {
    mockFetch({
      'GET /api/models/catalog': { body: catalog({}, ['chat-qwen3.5-4b', 'llama-cpu']) },
      'GET /api/models/install': noJob,
      'GET /api/server': { status: 500, body: {} },
      'POST /api/models/chat': {
        body: { source: 'local', model: 'qwen3.5-4b', base_url: null, restart_needed: true },
      },
    })
    renderPanel()
    fireEvent.click(within(await row('Qwen3.5 4B')).getByRole('button', { name: 'Use this: Qwen3.5 4B' }))
    expect(await within(chatSection()).findByText('Restart Hearthkeep to use it.')).toBeInTheDocument()
  })

  it('external server form takes an optional model name and shows where /v1 goes', async () => {
    const { calls } = modelRoutes(catalog(), {
      'POST /api/models/chat': {
        body: { source: 'external', model: 'qwen2.5:7b', base_url: 'http://127.0.0.1:11434/v1', restart_needed: true },
      },
    })
    renderPanel()
    const form = await screen.findByRole('form', { name: 'Use a model server I already run' })
    const address = within(form).getByLabelText('Server address')
    expect(address).toHaveAttribute('placeholder', 'http://127.0.0.1:11434/v1')
    const model = within(form).getByLabelText('Model name (optional)')
    expect(within(form).getByText("The model's name on that server, if it asks for one.")).toBeInTheDocument()
    fireEvent.change(address, { target: { value: 'http://127.0.0.1:11434/v1' } })
    fireEvent.change(model, { target: { value: '  qwen2.5:7b  ' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Use this server' }))
    expect(await within(chatSection()).findByText('In use: A model server you run')).toBeInTheDocument()
    const post = calls.find((c) => c.path === '/api/models/chat')!
    expect(bodyOf(post)).toMatchObject({ base_url: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:7b', api_key: null })
    expect(forbiddenIn(form.textContent ?? '')).toEqual([])
  })

  it('a model name the server refuses has its sentence', async () => {
    modelRoutes(catalog(), { 'POST /api/models/chat': { status: 400, body: { reason: 'bad_model' } } })
    renderPanel()
    const form = await screen.findByRole('form', { name: 'Use a model server I already run' })
    fireEvent.change(within(form).getByLabelText('Server address'), { target: { value: 'http://127.0.0.1:11434/v1' } })
    fireEvent.change(within(form).getByLabelText('Model name (optional)'), { target: { value: 'say "hi"' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Use this server' }))
    const alert = await within(chatSection()).findByRole('alert')
    expect(alert).toHaveTextContent(MODELS_ERRORS.bad_model)
  })

  it('the confirm box copy passes forbiddenIn', async () => {
    modelRoutes(catalog())
    renderPanel()
    fireEvent.click(within(await row('Gemma 4 12B')).getByRole('button', { name: 'Download Gemma 4 12B' }))
    const confirm = await screen.findByRole('group', { name: 'Ready to download' })
    const total = within(confirm).getByText(/^Up to /)
    const becomes = within(confirm).getByText('It becomes the chat model in use after a restart.')
    for (const text of [total.textContent ?? '', becomes.textContent ?? '', confirm.textContent ?? '']) {
      expect(text).not.toBe('')
      expect(forbiddenIn(text)).toEqual([])
    }
  })

  it('a failed server change clears the key too', async () => {
    modelRoutes(catalog(), { 'POST /api/models/chat': { status: 400, body: { reason: 'bad_url' } } })
    renderPanel()
    const form = await screen.findByRole('form', { name: 'Use a model server I already run' })
    const key = within(form).getByLabelText('Key (optional)')
    fireEvent.change(within(form).getByLabelText('Server address'), { target: { value: 'http://example.com/v1' } })
    fireEvent.change(key, { target: { value: 'sk-very-secret' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Use this server' }))
    await within(chatSection()).findByRole('alert')
    expect(key).toHaveValue('')
  })

  it('every new string passes forbiddenIn', async () => {
    for (const text of [...Object.values(ITEM_ERRORS), ...Object.values(MODELS_ERRORS)]) {
      expect(forbiddenIn(text)).toEqual([])
    }
    const everyError = job(
      Object.keys(ITEM_ERRORS).map((code, n) =>
        jobItem({ id: `i${n}`, name: `Item ${n}`, state: code === 'cancelled' ? 'cancelled' : 'failed', error: code }),
      ),
      { state: 'failed' },
    )
    modelRoutes(catalog({ pc: NO_GPU, supervised: false }, ['chat-qwen3.5-4b']), {
      'GET /api/models/install': { body: { job: everyError } },
    })
    const { container } = renderPanel(60_000)
    await screen.findByRole('region', { name: 'Downloads' })
    expect(forbiddenIn(container.textContent ?? '')).toEqual([])
    for (const a of addonList({ chat: 'not_set_up', images: 'not_set_up' })) {
      expect(forbiddenIn(a.message)).toEqual([])
    }
  })
})
