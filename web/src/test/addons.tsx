import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { vi } from 'vitest'
import type { Addon } from '../api/addons'
import { AddonsProvider, type SettingsAnchor } from '../shell/AddonsContext'

/** Render `ui` with these add-ons known; `openSettings` records where views send the owner. */
export function renderWithAddons(
  ui: ReactElement,
  addons: Addon[],
  openSettings: (anchor: SettingsAnchor) => void = vi.fn(),
) {
  const value = {
    addons,
    failed: false,
    refreshing: false,
    reload: vi.fn(),
    refresh: vi.fn(async () => {}),
    openSettings,
  }
  return { openSettings, ...render(<AddonsProvider value={value}>{ui}</AddonsProvider>) }
}
