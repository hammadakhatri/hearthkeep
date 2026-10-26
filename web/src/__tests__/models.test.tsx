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

describe('Models list', () => {
  it('lists models with kind and status', async () => {
    mockFetch({ 'GET /api/models': { body: base } })
    render(<ModelList />)
    const chat = (await screen.findByText('Local chat 12B')).closest('li')!
    expect(within(chat).getByText('Chat')).toBeInTheDocument()
    expect(within(chat).getByText('tools')).toBeInTheDocument()
    expect(within(chat).getByText('vision')).toBeInTheDocument()
    expect(within(chat).getByText('Shipped')).toBeInTheDocument()
    expect(within(chat).getByText('Loaded')).toBeInTheDocument()
    const img = screen.getByText('Picture maker').closest('li')!
    expect(within(img).getByText('Images')).toBeInTheDocument()
    expect(within(img).getByText('Yours')).toBeInTheDocument()
    expect(within(img).getByText('Ready')).toBeInTheDocument()
  })

  it('shows card errors', async () => {
    mockFetch({
      'GET /api/models': {
        body: {
          ...base,
          errors: [{ id_or_file: 'broken.toml', message: 'vram_gb is missing' }],
        },
      },
    })
    render(<ModelList />)
    const item = (await screen.findByText('broken.toml')).closest('li')!
    expect(item).toHaveTextContent('broken.toml vram_gb is missing')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows gpu usage', async () => {
    mockFetch({
      'GET /api/models': {
        body: { ...base, gpu: { ...base.gpu, waiting: 2 } },
      },
    })
    render(<ModelList />)
    expect(await screen.findByText('9.3 of 24.0 GB in use · 2 waiting')).toBeInTheDocument()
    const meter = screen.getByRole('meter')
    expect(meter).toHaveAttribute('aria-valuenow', '9.25')
    expect(meter).toHaveAttribute('aria-valuemin', '0')
    expect(meter).toHaveAttribute('aria-valuemax', '24')
    expect(meter).toHaveAttribute('aria-valuetext', '9.3 of 24.0 GB in use')
  })

  it('hides the gpu bar when the total is 0', async () => {
    mockFetch({
      'GET /api/models': {
        body: { ...base, gpu: { ...base.gpu, total_gb: 0, used_gb: 0 } },
      },
    })
    render(<ModelList />)
    await screen.findByText('Local chat 12B')
    expect(screen.queryByRole('meter')).toBeNull()
  })

  it('says so when there are no models', async () => {
    mockFetch({ 'GET /api/models': { body: { ...base, models: [] } } })
    render(<ModelList />)
    expect(await screen.findByText('No models yet.')).toBeInTheDocument()
    expect(screen.getByText(/add a card file/)).toBeInTheDocument()
  })

  it("shows a quiet line with Retry when models can't load", async () => {
    let fail = true
    mockFetch({
      'GET /api/models': () => (fail ? { status: 500, body: {} } : { body: base }),
    })
    render(<ModelList />)
    expect(await screen.findByText(/Couldn't load models\./)).toBeInTheDocument()
    fail = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Local chat 12B')).toBeInTheDocument()
  })
})

describe('status pill', () => {
  it('status pill describes gpu use', async () => {
    mockFetch({
      'GET /api/health': healthy,
      'GET /api/models': { body: base },
    })
    render(<AppShell onSignOut={() => {}} />)
    const pill = await screen.findByRole('status')
    await waitFor(() => expect(pill).toHaveAttribute('title', expect.stringContaining('GPU 9.3 of 24.0 GB in use')))
    const desc = document.getElementById(pill.getAttribute('aria-describedby')!)
    expect(desc).toHaveTextContent('GPU 9.3 of 24.0 GB in use')
  })

  it('a models failure does not change the health state', async () => {
    mockFetch({
      'GET /api/health': healthy,
      'GET /api/models': { status: 500, body: {} },
    })
    render(<AppShell onSignOut={() => {}} />)
    const pill = await screen.findByText('Local model 12B', {
      selector: '.status-text',
    })
    expect(pill.closest('[role="status"]')).toHaveAttribute('data-state', 'online')
    expect(pill.closest('[role="status"]')).not.toHaveAttribute('aria-describedby')
  })
})
