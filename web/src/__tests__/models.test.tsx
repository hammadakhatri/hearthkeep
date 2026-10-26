import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ModelsResponse } from '../api/models'
import { AppShell } from '../shell/AppShell'
import { ModelList } from '../components/ModelList'
import { healthy, mockFetch } from '../test/fetch'

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
const DOCS = 'To add a model, add a card file. See docs/models.md in the Hearthkeep repository.'

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
