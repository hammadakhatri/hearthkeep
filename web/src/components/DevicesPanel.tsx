import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { api, isApiError, post } from '../api/client'
import { passkeyErrorMessage, stepUp, type Device } from '../api/webauthn'
import { AlertIcon, InfoIcon, PasskeyIcon } from '../shell/icons'
import { QrCode } from './QrCode'

export type DeviceInfo = Device & { last_used_at?: string | null }

type AddCode = { code: string; expires_in_minutes: number }

const ONLY_DEVICE = "You can't remove your only device. Add another first."
const NAME_MAX = 64

/** "Added 1 Oct 2026". */
export function shortDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

/** "just now", "5 minutes ago", "yesterday", "3 days ago", then the date. */
export function relativeTime(iso: string, now = Date.now()): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const seconds = Math.round((now - then) / 1000)
  if (seconds < 60) return 'just now'
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return format.format(-minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (hours < 24) return format.format(-hours, 'hour')
  const days = Math.round(hours / 24)
  if (days < 30) return format.format(-days, 'day')
  return shortDate(iso)
}

function failure(error: unknown, action: string): string {
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  if (isApiError(error, 'step_up_required')) return "Your passkey couldn't be confirmed. Try again."
  if (isApiError(error, 'last_device')) return ONLY_DEVICE
  if (isApiError(error)) return `${action} didn't work. Try again.`
  return passkeyErrorMessage(error, action)
}

function Problem({ children }: { children: string }) {
  return (
    <div className="note error" role="alert">
      <AlertIcon />
      <span>{children}</span>
    </div>
  )
}

function DeviceRow({
  device,
  only,
  onChanged,
  onRemoved,
}: {
  device: DeviceInfo
  only: boolean
  onChanged: (d: DeviceInfo) => void
  onRemoved: (id: string) => void
}) {
  const [renaming, setRenaming] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [name, setName] = useState(device.device_name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const field = useRef<HTMLInputElement>(null)
  const onlyHint = useId()

  useEffect(() => {
    if (renaming) field.current?.select()
  }, [renaming])

  async function save(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const saved = await api<DeviceInfo>(`/api/devices/${encodeURIComponent(device.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ device_name: name.trim() }),
      })
      onChanged(saved)
      setRenaming(false)
    } catch (e) {
      setError(failure(e, 'Renaming'))
    }
    setBusy(false)
  }

  async function remove() {
    setBusy(true)
    setError(null)
    try {
      await api(`/api/devices/${encodeURIComponent(device.id)}`, { method: 'DELETE' })
      onRemoved(device.id)
      return
    } catch (e) {
      setError(failure(e, 'Removing'))
    }
    setBusy(false)
    setConfirming(false)
  }

  return (
    <li className="device" aria-busy={busy || undefined}>
      {renaming ? (
        <form className="device-rename" onSubmit={save}>
          <label className="field">
            <span>Device name</span>
            <input
              ref={field}
              className="input"
              value={name}
              maxLength={NAME_MAX}
              autoComplete="off"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <div className="device-actions">
            <button type="submit" className="btn primary" disabled={busy}>
              Save
            </button>
            <button
              type="button"
              className="btn ghost"
              disabled={busy}
              onClick={() => {
                setRenaming(false)
                setName(device.device_name)
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="device-main">
          <div className="device-text">
            <p className="device-name">
              <b>{device.device_name}</b>
              {device.current && <span className="badge green">This device</span>}
            </p>
            <p className="device-meta">
              <span>Added {shortDate(device.created_at)}</span>
              {device.last_used_at && <span>Last used {relativeTime(device.last_used_at)}</span>}
            </p>
          </div>
          {confirming ? (
            <div className="device-actions" role="group" aria-label={`Remove ${device.device_name}?`}>
              <span className="device-confirm">
                {device.current ? 'This signs this browser out.' : 'It will need a new code to come back.'}
              </span>
              <button type="button" className="btn danger" disabled={busy} onClick={remove}>
                Yes, remove
              </button>
              <button type="button" className="btn ghost" disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </div>
          ) : (
            <div className="device-actions">
              <button
                type="button"
                className="btn ghost"
                aria-label={`Rename ${device.device_name}`}
                onClick={() => {
                  setName(device.device_name)
                  setRenaming(true)
                }}
              >
                Rename
              </button>
              <button
                type="button"
                className="btn ghost"
                aria-label={`Remove ${device.device_name}`}
                disabled={only}
                aria-describedby={only ? onlyHint : undefined}
                onClick={() => setConfirming(true)}
              >
                Remove
              </button>
            </div>
          )}
        </div>
      )}
      {only && !renaming && (
        <p className="hint device-only" id={onlyHint}>
          {ONLY_DEVICE}
        </p>
      )}
      {error && <Problem>{error}</Problem>}
    </li>
  )
}

/** Devices with a passkey for Hearthkeep: rename, remove, add one, sign out the others. */
export function DevicesPanel() {
  const [devices, setDevices] = useState<DeviceInfo[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [code, setCode] = useState<AddCode | null>(null)
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [signingOut, setSigningOut] = useState(false)
  const [signedOut, setSignedOut] = useState<string | null>(null)
  const [signOutError, setSignOutError] = useState<string | null>(null)
  const titleId = useId()

  const load = useCallback(() => {
    setFailed(false)
    api<DeviceInfo[]>('/api/devices')
      .then(setDevices)
      .catch(() => setFailed(true))
  }, [])

  useEffect(load, [load])

  async function addDevice() {
    setAdding(true)
    setAddError(null)
    try {
      const token = await stepUp('add_device')
      setCode(await api<AddCode>('/api/devices/add-code', post({ step_up_token: token })))
    } catch (e) {
      setAddError(failure(e, 'Adding a device'))
    }
    setAdding(false)
  }

  async function signOutOthers() {
    setSigningOut(true)
    setSignedOut(null)
    setSignOutError(null)
    try {
      const { signed_out: n } = await api<{ signed_out: number }>('/api/sessions/sign-out-others', post({}))
      setSignedOut(n === 1 ? 'Signed out 1 other session.' : `Signed out ${n} other sessions.`)
      load()
    } catch (e) {
      setSignOutError(failure(e, 'Signing out'))
    }
    setSigningOut(false)
  }

  const link = code ? `${window.location.origin}/#enroll=${code.code}` : ''

  return (
    <section className="settings-section" aria-labelledby={titleId}>
      <h2 id={titleId}>Devices</h2>
      <p className="settings-lede">Each device signs in with its own passkey: Face ID, Touch ID or a PIN.</p>
      {failed ? (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>Couldn't load your devices.</span>
          <button type="button" className="btn ghost" onClick={load}>
            Retry
          </button>
        </div>
      ) : devices === null ? (
        <div className="device-list skeleton" aria-busy="true" aria-label="Loading devices">
          <span className="bar w30" />
          <span className="bar w60" />
        </div>
      ) : (
        <ul className="device-list" aria-label="Devices">
          {devices.map((device) => (
            <DeviceRow
              key={device.id}
              device={device}
              only={devices.length === 1}
              onChanged={(saved) =>
                setDevices((all) => all?.map((d) => (d.id === saved.id ? { ...d, ...saved } : d)) ?? null)
              }
              onRemoved={(id) => setDevices((all) => all?.filter((d) => d.id !== id) ?? null)}
            />
          ))}
        </ul>
      )}

      {code && (
        <div className="tile add-code" role="status">
          <QrCode text={link} />
          <div className="add-code-text">
            <p className="perm-sub">On the new device, scan this or open Hearthkeep and enter the code.</p>
            <p className="add-code-value">{code.code}</p>
            <p className="add-code-link">{link}</p>
            <p className="perm-sub">Valid for {code.expires_in_minutes} minutes</p>
            <button type="button" className="btn ghost" onClick={() => setCode(null)}>
              Done
            </button>
          </div>
        </div>
      )}
      <div className="settings-actions">
        {!code && (
          <button type="button" className="btn primary" onClick={addDevice} disabled={adding} aria-busy={adding}>
            <PasskeyIcon />
            {adding ? 'Waiting for your passkey…' : 'Add a device'}
          </button>
        )}
        <button type="button" className="btn ghost" onClick={signOutOthers} disabled={signingOut}>
          Sign out other sessions
        </button>
      </div>
      {addError && <Problem>{addError}</Problem>}
      {signOutError && <Problem>{signOutError}</Problem>}
      {signedOut && (
        <div className="note info" role="status">
          <InfoIcon />
          <span>{signedOut}</span>
        </div>
      )}
    </section>
  )
}
