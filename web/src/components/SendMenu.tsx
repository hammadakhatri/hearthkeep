import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { isApiError } from '../api/client'
import { listDevices, sendItem, type DeviceInfo, type SentKind } from '../api/sent'
import { AlertIcon, SentIcon } from '../shell/icons'

export const SENT_LINE = 'Sent.'
export const DEVICES_FAILED = "Couldn't load your devices; you can still send to all of them."
export const TOO_LONG = 'Too long to send as a summary. Send the chart or rows instead.'

/** The server's own message when it gave one, else a plain sentence for its reason. */
export function sendErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    const message = (error.body as { message?: unknown } | null)?.message
    if (typeof message === 'string' && message) return message
  }
  if (isApiError(error, 'too_long')) return TOO_LONG
  if (isApiError(error, 'not_found')) return "That can't be sent any more. It may have been deleted."
  if (isApiError(error, 'unknown_device')) return "That device isn't signed in any more."
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  return "That couldn't be sent. Try again."
}

/**
 * "Send" on a chart, table, file or summary: a menu button with "Send to all my devices" and
 * one entry per device. `refId` is the output or file id, or for a summary the assistant
 * message id (the server takes the text itself). Device names are shown as text.
 */
export function SendMenu({ kind, refId, label }: { kind: SentKind; refId: string; label: string }) {
  const menuId = useId()
  const button = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const root = useRef<HTMLSpanElement>(null)
  const [open, setOpen] = useState(false)
  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [devicesFailed, setDevicesFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)
  // Which entry gets the focus when the menu opens: 0 (first) or -1 (last).
  const focusAt = useRef<0 | -1>(0)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // The device list is fetched each time the menu opens, so it never shows a removed device.
  useEffect(() => {
    if (!open) return
    let live = true
    setDevicesFailed(false)
    listDevices().then(
      (list) => live && setDevices(list),
      () => {
        // "all my devices" still works
        if (!live) return
        setDevices([])
        setDevicesFailed(true)
      },
    )
    return () => {
      live = false
    }
  }, [open])

  function entries(): HTMLElement[] {
    return [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
  }

  // Focus follows the menu opening and the device list arriving (the last entry stays last).
  useEffect(() => {
    if (!open) return
    const list = entries()
    if (!list.length) return
    const active = document.activeElement
    if (active && menu.current?.contains(active)) return
    list.at(focusAt.current)?.focus()
  }, [open, devices])

  useEffect(() => {
    if (!open) return
    const away = (event: Event) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [open])

  function openMenu(at: 0 | -1) {
    focusAt.current = at
    setStatus(null)
    setError(null)
    setOpen(true)
  }

  function close(restoreFocus: boolean) {
    setOpen(false)
    if (restoreFocus) button.current?.focus()
  }

  function onButtonKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      openMenu(0)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      openMenu(-1)
    }
  }

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const list = entries()
    const at = list.indexOf(document.activeElement as HTMLElement)
    const go = (to: number) => {
      event.preventDefault()
      list[(to + list.length) % list.length]?.focus()
    }
    switch (event.key) {
      case 'ArrowDown':
        return go(at + 1)
      case 'ArrowUp':
        return go(at < 0 ? -1 : at - 1)
      case 'Home':
        return go(0)
      case 'End':
        return go(-1)
      case 'Escape':
        event.preventDefault()
        event.stopPropagation()
        return close(true)
      case 'Tab':
        return close(false)
    }
  }

  async function send(to: 'all' | string[]) {
    if (busy) return
    setOpen(false)
    setBusy(true)
    setStatus(null)
    setError(null)
    try {
      await sendItem({ kind, ref_id: refId, devices: to })
      if (alive.current) setStatus(SENT_LINE)
    } catch (e) {
      if (alive.current) setError(sendErrorMessage(e))
    }
    if (!alive.current) return
    setBusy(false)
    // A disabled button drops the focus; give it back once the send is over.
    window.setTimeout(() => button.current?.focus(), 0)
  }

  return (
    <span className="send" ref={root}>
      <button
        ref={button}
        type="button"
        className="btn send-button"
        aria-label={`Send ${label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={busy}
        onClick={() => (open ? close(false) : openMenu(0))}
        onKeyDown={onButtonKeyDown}
      >
        <SentIcon />
        Send
      </button>
      {open && (
        <div className="send-pop">
        <div ref={menu} id={menuId} className="send-menu" role="menu" aria-label={`Send ${label} to`} onKeyDown={onMenuKeyDown}>
          <button type="button" role="menuitem" tabIndex={-1} className="send-item" onClick={() => void send('all')}>
            Send to all my devices
          </button>
          {devices.map((device) => (
            <button
              key={device.id}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="send-item"
              onClick={() => void send([device.id])}
            >
              <bdi>{device.device_name}</bdi>
            </button>
          ))}
        </div>
        {devicesFailed && <p className="send-note">{DEVICES_FAILED}</p>}
        </div>
      )}
      <span className="send-status" role="status">
        {status}
      </span>
      {error && (
        <span className="note error send-error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </span>
      )}
    </span>
  )
}
