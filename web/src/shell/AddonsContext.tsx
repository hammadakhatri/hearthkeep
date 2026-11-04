import { createContext, useContext, type ReactNode } from 'react'
import type { Addon, AddonId, AddonsState } from '../api/addons'

/** Where in Settings the owner is sent: a section, or one add-on's card. */
export type SettingsAnchor =
  | 'settings-addons'
  | `addon-${AddonId}`
  | 'settings-chat'
  | 'settings-images'
  | 'settings-github'
  | 'settings-folders'
  | 'settings-startup'
  | 'settings-general'

type AddonsValue = AddonsState & { openSettings(anchor: SettingsAnchor): void }

const AddonsContext = createContext<AddonsValue | null>(null)

export function AddonsProvider({ value, children }: { value: AddonsValue; children: ReactNode }) {
  return <AddonsContext.Provider value={value}>{children}</AddonsContext.Provider>
}

export function useAddonsState(): AddonsState | null {
  return useContext(AddonsContext)
}

/** One add-on, or null when unknown (no provider, still loading, or the load failed).
 *  Unknown never gates anything: views behave as they did before add-ons. */
export function useAddon(id: AddonId): Addon | null {
  return useContext(AddonsContext)?.addons?.find((a) => a.id === id) ?? null
}

/** Open Settings at an anchor. A no-op without a provider. */
export function useOpenSettings(): (anchor: SettingsAnchor) => void {
  return useContext(AddonsContext)?.openSettings ?? noop
}

function noop() {}

/** The screen that can act on an add-on's fix. Setting up chat and installing images lead to
 *  Settings › Chat model and Image models; every action without such a screen leads to the
 *  add-on's own card. */
export function actionTarget(addon: Addon): SettingsAnchor {
  if (addon.id === 'chat' && addon.action === 'open_setup') return 'settings-chat'
  if (addon.id === 'images' && addon.action === 'install') return 'settings-images'
  if (addon.action === 'add_token') return 'settings-github'
  if (addon.action === 'turn_on') return 'settings-startup'
  return `addon-${addon.id}`
}
