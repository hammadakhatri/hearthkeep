import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Addon } from '../api/addons'
import { SetUpPanel } from '../components/SetUpPanel'
import { Composer } from '../shell/Composer'
import { TopBar, type HealthState } from '../shell/TopBar'
import { Analysis } from '../views/Analysis'
import { Chat } from '../views/Chat'
import { Images } from '../views/Images'
import { Repos } from '../views/Repos'
import { ToolsPermissions } from '../views/ToolsPermissions'
import { renderWithAddons } from '../test/addons'
import { forbiddenIn } from '../test/copyRules'
import { FakeEventSource, installEventSource } from '../test/eventsource'
import { addonList, mockFetch } from '../test/fetch'

vi.mock('vega-embed', () => ({ default: vi.fn() }))

const addons = (states: Partial<Record<string, string>> = {}) => addonList(states) as Addon[]
const addon = (id: string, states: Partial<Record<string, string>> = {}) => addons(states).find((a) => a.id === id)!

const CHAT = { id: 'c1', title: '', think: false, created_at: '2026-10-26T10:00:00Z', active_turn: null }

beforeEach(() => {
  installEventSource()
})

function chatRoutes(extra: Parameters<typeof mockFetch>[0] = {}) {
  return mockFetch({
    'GET /api/chats': { body: [] },
    'POST /api/chats': { status: 201, body: CHAT },
    'POST /api/chats/c1/messages': { status: 202, body: { turn_id: 't1' } },
    ...extra,
  })
}

describe('the composer follows the chat add-on', () => {
  it('is disabled with visible text when no chat model is set up', async () => {
    chatRoutes()
    const { openSettings } = renderWithAddons(<Chat model={null} />, addons({ chat: 'not_set_up' }))
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    expect(box).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    const why = screen.getByText('Set up your chat model to start chatting.')
    expect(why).toBeVisible()
    expect(box).toHaveAttribute('aria-describedby', expect.stringContaining('composer-state'))
    expect(why.closest('#composer-state')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Set up' }))
    expect(openSettings).toHaveBeenCalledWith('addon-chat')
  })

  it('an offline chat server disables the composer with visible text, not a tooltip', async () => {
    chatRoutes()
    renderWithAddons(<Chat model={null} />, addons({ chat: 'offline' }))
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    expect(box).toBeDisabled()
    expect(screen.getByText("The chat server isn't answering. Make sure it's running on this PC.")).toBeVisible()
  })

  it('missing model files disable the composer too', async () => {
    chatRoutes()
    renderWithAddons(<Chat model={null} />, addons({ chat: 'error' }))
    expect(await screen.findByRole('textbox', { name: 'Message Hearthkeep' })).toBeDisabled()
    expect(screen.getByText("Hearthkeep can't find the chat model's files on this PC.")).toBeVisible()
  })

  it('a model that is loading shows a status and stays usable', async () => {
    chatRoutes()
    renderWithAddons(<Chat model={null} />, addons({ chat: 'starting' }))
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    expect(box).toBeEnabled()
    const note = screen.getByText('Loading the chat model…')
    expect(note.closest('#composer-state')).toHaveAttribute('role', 'status')
  })

  it('the first send while the model is not loaded shows "Loading the chat model…"', async () => {
    chatRoutes()
    const list = addons()
    list[0] = { ...list[0], details: { model: 'Local model 12B', loaded: false } }
    renderWithAddons(<Chat model="Local model 12B" />, list)
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    fireEvent.change(box, { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const typing = screen.getByRole('status', { name: 'Hearthkeep is writing' })
    expect(typing.parentElement).toHaveTextContent('Loading the chat model…')
  })

  it('no loading text when the model is already loaded', async () => {
    chatRoutes()
    renderWithAddons(<Chat model="Local model 12B" />, addons())
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    fireEvent.change(box, { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(screen.queryByText('Loading the chat model…')).toBeNull()
  })

  it('a 409 chat_not_set_up shows the server message', async () => {
    chatRoutes({
      'POST /api/chats/c1/messages': {
        status: 409,
        body: { reason: 'chat_not_set_up', message: 'Set up your chat model to start chatting.' },
      },
    })
    // Add-ons unknown (no provider): the composer is usable and the server answers for it.
    render(<Chat model={null} />)
    const box = await screen.findByRole('textbox', { name: 'Message Hearthkeep' })
    fireEvent.change(box, { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Set up your chat model to start chatting.')
  })

  it('Composer: a notice keeps it usable and is announced', () => {
    render(
      <Composer
        onSend={async () => true}
        busy={false}
        think={false}
        onThinkChange={() => {}}
        thinkLocked={false}
        model={null}
        notice="Loading the chat model…"
      />,
    )
    expect(screen.getByRole('textbox')).toBeEnabled()
    expect(screen.getByRole('status')).toHaveTextContent('Loading the chat model…')
  })
})

function pill(ui: ReturnType<typeof render>) {
  return ui.container.querySelector('.status') as HTMLElement
}

const online: HealthState = { state: 'online', model: 'Local model 12B' }

describe('the top bar pill', () => {
  it.each([
    [{ state: 'checking' } as HealthState, 'Checking…', 'checking'],
    [online, 'Local model 12B', 'online'],
    [{ state: 'standby', model: 'Local model 12B' } as HealthState, 'Local model 12B · starts when needed', 'standby'],
    [{ state: 'offline', model: 'Local model 12B' } as HealthState, 'Chat model offline', 'offline'],
    [{ state: 'not_set_up', model: '' } as HealthState, 'No chat model', 'not_set_up'],
    [{ state: 'unreachable' } as HealthState, "Can't reach Hearthkeep", 'unreachable'],
  ])('keeps the health behaviour with no add-ons known: %j', (health, text, state) => {
    const ui = render(<TopBar title="Chat" health={health} />)
    expect(within(pill(ui)).getByText(text)).toBeInTheDocument()
    expect(pill(ui)).toHaveAttribute('data-state', state)
  })

  it.each([
    ['not_set_up', 'No chat model', 'not_set_up'],
    ['starting', 'Loading the chat model…', 'standby'],
    ['offline', 'Chat model offline', 'offline'],
    ['error', 'Chat model needs attention', 'offline'],
    ['ready', 'Local model 12B', 'online'],
  ])('follows the chat add-on: %s', (chat, text, state) => {
    const ui = renderWithAddons(<TopBar title="Chat" health={online} />, addons({ chat }))
    expect(within(pill(ui)).getByText(text)).toBeInTheDocument()
    expect(pill(ui)).toHaveAttribute('data-state', state)
  })

  it('says a ready model that is not loaded starts when needed', () => {
    const list = addons()
    list[0] = { ...list[0], details: { model: 'Local model 12B', loaded: false } }
    const ui = renderWithAddons(<TopBar title="Chat" health={online} />, list)
    expect(pill(ui)).toHaveTextContent('Local model 12B · starts when needed')
    expect(pill(ui)).toHaveAttribute('data-state', 'standby')
  })

  it('health says standby but the chat add-on has an error: the pill shows the error', () => {
    const health: HealthState = { state: 'standby', model: 'Local model 12B' }
    const ui = renderWithAddons(<TopBar title="Chat" health={health} />, addons({ chat: 'error' }))
    expect(pill(ui)).toHaveTextContent('Chat model needs attention')
    expect(pill(ui)).toHaveAttribute('data-state', 'offline')
    expect(pill(ui)).not.toHaveTextContent(/starts when needed|ready/i)
  })

  it('an unreachable server still shows as unreachable', () => {
    const ui = renderWithAddons(<TopBar title="Chat" health={{ state: 'unreachable' }} />, addons())
    expect(pill(ui)).toHaveAttribute('data-state', 'unreachable')
  })
})

describe('Images', () => {
  const noModels = { body: { models: [], errors: [], gpu: { resident: [] } } }

  it('without its add-on shows one set-up panel and no form', async () => {
    mockFetch({ 'GET /api/models': noModels, 'GET /api/images?limit=24': { body: { images: [] } } })
    const { openSettings } = renderWithAddons(<Images />, addons({ images: 'not_set_up' }))
    expect(screen.queryByRole('textbox', { name: 'Describe the image you want' })).toBeNull()
    const panel = screen.getByRole('region', { name: 'Set up Images' })
    expect(within(panel).getByText('Create and edit images on this PC.')).toBeInTheDocument()
    expect(within(panel).getByText('Needs: ComfyUI and at least one image model.')).toBeInTheDocument()
    fireEvent.click(within(panel).getByRole('button', { name: 'Set up Images' }))
    expect(openSettings).toHaveBeenCalledWith('addon-images')
    expect(screen.queryByText(/docs\/models\.md/)).toBeNull()
  })

  it('keeps saved images visible under the panel', async () => {
    mockFetch({
      'GET /api/models': noModels,
      'GET /api/images?limit=24': {
        body: {
          images: [
            { id: 'img1', model_id: 'z', prompt: 'A red kite', width: 1024, height: 1024, created_at: '2026-10-26T10:00:00Z' },
          ],
        },
      },
    })
    renderWithAddons(<Images />, addons({ images: 'error' }))
    expect(await screen.findByRole('button', { name: /A red kite/ })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Set up Images' })).toBeInTheDocument()
  })
})

describe('Analysis', () => {
  const rows = {
    chats: [
      {
        chat_id: 'a'.repeat(32),
        title: 'Shop sales',
        updated_at: '2026-10-27T10:00:00.000000Z',
        files: [
          {
            id: 'f1',
            chat_id: 'a'.repeat(32),
            name: 'sales.csv',
            kind: 'csv',
            size: 2048,
            status: 'ready',
            created_at: '2026-10-26T10:00:00Z',
          },
        ],
        charts: [],
      },
    ],
    more: false,
  }

  it('the empty state has a Start a chat with a file button', async () => {
    mockFetch({ 'GET /api/analysis?limit=20': { body: { chats: [], more: false } } })
    const onStartChat = vi.fn()
    renderWithAddons(<Analysis onOpenChat={() => {}} onStartChat={onStartChat} />, addons())
    expect(
      await screen.findByText('Attach a file to a chat to start. Your files and charts collect here.'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Start a chat with a file' }))
    expect(onStartChat).toHaveBeenCalledOnce()
  })

  it('without Docker shows the set-up panel above the saved files', async () => {
    mockFetch({ 'GET /api/analysis?limit=20': { body: rows } })
    const { openSettings } = renderWithAddons(
      <Analysis onOpenChat={() => {}} onStartChat={() => {}} />,
      addons({ analysis: 'docker_stopped' }),
    )
    const panel = screen.getByRole('region', { name: 'Set up Analysis' })
    const saved = await screen.findByRole('region', { name: 'Shop sales' })
    expect(panel.compareDocumentPosition(saved) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(panel).getByText("Docker Desktop isn't running. Start it, then check again.")).toBeInTheDocument()
    fireEvent.click(within(panel).getByRole('button', { name: 'Set up Analysis' }))
    expect(openSettings).toHaveBeenCalledWith('addon-analysis')
  })
})

describe('Repos', () => {
  it('needing a token shows the set-up panel, no add form, and no Update', async () => {
    mockFetch({
      'GET /api/github': { body: { git: true, connected: false, login: null } },
      'GET /api/repos': {
        body: {
          repos: [
            {
              id: 'r1',
              full_name: 'octo/widgets',
              default_branch: 'main',
              private: true,
              branch: 'main',
              changed: [],
              ahead: 0,
              behind: 0,
              deps: 'none',
              last_run: null,
            },
          ],
        },
      },
    })
    const { openSettings } = renderWithAddons(<Repos />, addons({ github: 'needs_token' }))
    expect(await screen.findByText('octo/widgets')).toBeInTheDocument()
    const panel = screen.getByRole('region', { name: 'Set up GitHub' })
    expect(screen.queryByRole('textbox', { name: 'Repository' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Add repository' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update octo/widgets' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Remove octo/widgets' })).toBeInTheDocument()
    expect(screen.queryByText(/Connect GitHub in Settings|Install Git/)).toBeNull()
    fireEvent.click(within(panel).getByRole('button', { name: 'Set up GitHub' }))
    expect(openSettings).toHaveBeenCalledWith('settings-github')
  })
})

describe('Tools & permissions', () => {
  const python = {
    tool: 'python',
    level: 'allowed',
    effective_level: 'allowed',
    description: 'Run Python.',
    tools: [],
    addon: { id: 'analysis', state: 'docker_stopped', ready: false, short: 'Needs Docker', message: 'Docker is off.' },
  }
  const noModels = { 'GET /api/models': { status: 404, body: {} } }

  it('rows show "Needs Docker — Set up" and no placeholder rows', async () => {
    mockFetch({ 'GET /api/settings/tools': { body: [python] }, ...noModels })
    const { openSettings, container } = renderWithAddons(<ToolsPermissions />, addons({ analysis: 'docker_stopped' }))
    const row = (await screen.findByText('Python')).closest('li')!
    expect(row.querySelector('.perm-flag')).toHaveTextContent('Needs Docker — Set up')
    fireEvent.click(within(row).getByRole('button', { name: 'Set up' }))
    expect(openSettings).toHaveBeenCalledWith('addon-analysis')
    expect(container).not.toHaveTextContent(/Arrives in a later update|Face ID|Touch ID|Needs Docker to run freely/)
    expect(screen.queryByText('Wallet')).toBeNull()
  })

  it('a ready tool shows no flag', async () => {
    const ready = { ...python, addon: { ...python.addon, state: 'ready', ready: true, short: null } }
    mockFetch({ 'GET /api/settings/tools': { body: [ready] }, ...noModels })
    render(<ToolsPermissions />)
    const row = (await screen.findByText('Python')).closest('li')!
    expect(within(row).queryByRole('button', { name: 'Set up' })).toBeNull()
  })
})

describe('the set-up panel copy', () => {
  it('never names a command or shows a placeholder, for any add-on state', () => {
    const states = [
      { chat: 'not_set_up' },
      { chat: 'offline' },
      { chat: 'error' },
      { analysis: 'not_set_up' },
      { analysis: 'docker_stopped' },
      { analysis: 'needs_image' },
      { images: 'not_set_up' },
      { images: 'error' },
      { github: 'not_set_up' },
      { github: 'needs_git' },
      { github: 'needs_token' },
    ]
    for (const state of states) {
      const [id] = Object.keys(state)
      const { container, unmount } = renderWithAddons(<SetUpPanel addon={addon(id, state)} />, addons(state))
      expect(forbiddenIn(container.textContent ?? '')).toEqual([])
      expect(container.querySelector('button')).toHaveTextContent(`Set up ${addon(id, state).name}`)
      unmount()
    }
  })
})
