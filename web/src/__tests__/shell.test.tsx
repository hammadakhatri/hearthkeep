import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AppShell } from '../shell/AppShell'
import { healthy, mockFetch, standby, unreachable } from '../test/fetch'

const SEVEN = ['Today', 'Chat', 'Images', 'Analysis', 'Repos', 'Files', 'Tools & permissions']

describe('AppShell', () => {
  it('renders sidebar with seven destinations', () => {
    mockFetch({ 'GET /api/health': healthy })
    render(<AppShell onSignOut={() => {}} />)
    const sidebar = screen.getByRole('navigation', { name: 'Hearthkeep' })
    for (const name of SEVEN) {
      expect(within(sidebar).getByRole('button', { name })).toBeInTheDocument()
    }
    // Tools & permissions sits apart, at the bottom.
    const footer = within(sidebar).getByRole('button', { name: 'Tools & permissions' }).parentElement!
    expect(within(footer).queryByRole('button', { name: 'Chat' })).toBeNull()
  })

  it('opens Chat by default and switches view from the sidebar', () => {
    mockFetch({ 'GET /api/health': healthy })
    render(<AppShell onSignOut={() => {}} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Chat' })).toBeInTheDocument()
    const sidebar = screen.getByRole('navigation', { name: 'Hearthkeep' })
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Images' }))
    expect(screen.getByRole('heading', { level: 1, name: 'Images' })).toBeInTheDocument()
    expect(within(sidebar).getByRole('button', { name: 'Images' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('textbox', { name: 'Describe the image you want' })).toBeInTheDocument()
    fireEvent.click(within(sidebar).getByRole('button', { name: 'Repos' }))
    expect(screen.getByText('Arrives in a later update')).toBeInTheDocument()
  })

  it('status pill shows offline when health says unreachable', async () => {
    mockFetch({ 'GET /api/health': unreachable })
    render(<AppShell onSignOut={() => {}} />)
    const pill = await screen.findByText('Chat model offline')
    expect(pill.closest('[role="status"]')).toHaveAttribute('data-state', 'offline')
  })

  it('status pill shows the model name when reachable', async () => {
    mockFetch({ 'GET /api/health': healthy })
    render(<AppShell onSignOut={() => {}} />)
    // The composer names the model too; the pill is the one in the top bar.
    const pill = await screen.findByText('Local model 12B', { selector: '.status-text' })
    expect(pill.closest('[role="status"]')).toHaveAttribute('data-state', 'online')
  })

  it('status pill says a managed model starts when needed', async () => {
    mockFetch({ 'GET /api/health': standby })
    render(<AppShell onSignOut={() => {}} />)
    const pill = await screen.findByText('Local model 12B · starts when needed')
    expect(pill.closest('[role="status"]')).toHaveAttribute('data-state', 'standby')
  })

  it('status pill says the server is unreachable when health cannot be fetched', async () => {
    mockFetch({ 'GET /api/health': () => Promise.reject(new TypeError('Failed to fetch')) })
    render(<AppShell onSignOut={() => {}} />)
    expect(await screen.findByText("Can't reach Hearthkeep")).toBeInTheDocument()
  })

  it('bottom tabs offer Today, Chat, Images, Analysis and More, with the rest under More', () => {
    mockFetch({ 'GET /api/health': healthy })
    render(<AppShell onSignOut={() => {}} />)
    const tabs = screen.getByRole('navigation', { name: 'Tabs' })
    const names = within(tabs).getAllByRole('button').map((b) => b.textContent)
    expect(names).toEqual(['Today', 'Chat', 'Images', 'Analysis', 'More'])

    fireEvent.click(within(tabs).getByRole('button', { name: 'More' }))
    const sheet = screen.getByRole('dialog', { name: 'More' })
    fireEvent.click(within(sheet).getByRole('button', { name: 'Repos' }))
    expect(screen.queryByRole('dialog', { name: 'More' })).toBeNull()
    expect(screen.getByRole('heading', { level: 1, name: 'Repos' })).toBeInTheDocument()
    expect(within(tabs).getByRole('button', { name: 'More' })).toHaveAttribute('aria-current', 'page')
  })

  it('More sheet closes on Escape', () => {
    mockFetch({ 'GET /api/health': healthy })
    render(<AppShell onSignOut={() => {}} />)
    const tabs = screen.getByRole('navigation', { name: 'Tabs' })
    fireEvent.click(within(tabs).getByRole('button', { name: 'More' }))
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'More' }), { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'More' })).toBeNull()
  })
})
