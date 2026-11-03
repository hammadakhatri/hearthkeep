import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './client'

export type AddonId = 'chat' | 'analysis' | 'images' | 'github' | 'devices' | 'startup'
export type AddonAction = 'install' | 'start_docker' | 'build_image' | 'add_token' | 'open_setup' | 'turn_on'

export type Addon = {
  id: AddonId
  name: string
  about: string
  requires: string
  state: string
  ready: boolean
  message: string
  action: AddonAction | null
  short: string | null
  details: { model?: string; loaded?: boolean }
}

export type AddonsResponse = { addons: Addon[]; checked_at: string | null }

export function getAddons(): Promise<AddonsResponse> {
  return api<AddonsResponse>('/api/addons')
}

export function refreshAddons(): Promise<AddonsResponse> {
  return api<AddonsResponse>('/api/addons/refresh', { method: 'POST' })
}

export type AddonsState = {
  /** null until the first load succeeds; a failed load leaves it as it was. */
  addons: Addon[] | null
  failed: boolean
  refreshing: boolean
  reload(): void
  /** "Check again": the server looks afresh, then the list is replaced. */
  refresh(): Promise<void>
}

const POLL_MS = 15_000

/** Polls /api/addons (the server caches it) and re-checks when the tab returns. */
export function useAddons(): AddonsState {
  const [addons, setAddons] = useState<Addon[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const alive = useRef(true)

  const reload = useCallback(() => {
    getAddons()
      .then((r) => {
        if (!alive.current) return
        setAddons(r.addons)
        setFailed(false)
      })
      .catch(() => alive.current && setFailed(true))
  }, [])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const r = await refreshAddons()
      if (!alive.current) return
      setAddons(r.addons)
      setFailed(false)
    } catch {
      if (alive.current) setFailed(true)
    }
    if (alive.current) setRefreshing(false)
  }, [])

  useEffect(() => {
    alive.current = true
    const onVisible = () => {
      if (document.visibilityState === 'visible') reload()
    }
    reload()
    const timer = window.setInterval(reload, POLL_MS)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive.current = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [reload])

  return useMemo(
    () => ({ addons, failed, refreshing, reload, refresh }),
    [addons, failed, refreshing, reload, refresh],
  )
}
