import { useEffect, useId, useState } from 'react'
import { isApiError } from '../api/client'
import {
  NotificationsBlockedError,
  disableNotifications,
  enableNotifications,
  notificationsBlocked,
  notificationsEnabled,
  pushSupport,
} from '../push'
import { AlertIcon, InfoIcon } from '../shell/icons'

const BLOCKED = "Notifications are blocked for this site. Allow them in the browser's site settings."
const HOME_SCREEN = 'On iPhone, add Hearthkeep to your Home Screen first, then turn this on there.'

function errorMessage(error: unknown): string {
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Try again in a moment."
  if (isApiError(error, 'push_host_not_allowed')) return "This browser's notification service isn't one Hearthkeep sends to."
  return "That didn't work. Try again."
}

/** "Notifications on this device": a switch that subscribes this browser to payload-free push. */
export function NotificationToggle() {
  const labelId = useId()
  const hintId = useId()
  const [support] = useState(pushSupport)
  const [on, setOn] = useState<boolean | null>(support === 'supported' ? null : false)
  const [busy, setBusy] = useState(false)
  const [blocked, setBlocked] = useState(() => support === 'supported' && notificationsBlocked())
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (support !== 'supported') return
    let live = true
    notificationsEnabled()
      .then((enabled) => live && setOn(enabled))
      .catch(() => {
        if (!live) return
        setOn(false)
        setError("Couldn't check notifications on this device.")
      })
    return () => {
      live = false
    }
  }, [support])

  async function toggle() {
    setBusy(true)
    setError(null)
    try {
      if (on) {
        await disableNotifications()
        setOn(false)
      } else {
        await enableNotifications()
        setOn(true)
      }
    } catch (e) {
      if (e instanceof NotificationsBlockedError) setBlocked(true)
      else setError(errorMessage(e))
    }
    setBusy(false)
  }

  const checked = on === true
  return (
    <div className="tile notify" aria-busy={busy || on === null || undefined}>
      <div className="perm-text">
        <b id={labelId}>Notifications on this device</b>
        <p className="perm-sub" id={hintId}>
          Get a notice here when something is sent to this device. It says only "A file is ready in Hearthkeep".
        </p>
        {support === 'needs-home-screen' && (
          <div className="note info">
            <InfoIcon />
            <span>{HOME_SCREEN}</span>
          </div>
        )}
        {support === 'unsupported' && (
          <div className="note">
            <InfoIcon />
            <span>This browser can't show notifications from Hearthkeep.</span>
          </div>
        )}
        {blocked && (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>{BLOCKED}</span>
          </div>
        )}
        {error && (
          <div className="note error" role="alert">
            <AlertIcon />
            <span>{error}</span>
          </div>
        )}
      </div>
      <button
        type="button"
        role="switch"
        className="switch"
        aria-checked={checked}
        aria-labelledby={labelId}
        aria-describedby={hintId}
        disabled={support !== 'supported' || on === null || busy || blocked}
        onClick={toggle}
      >
        <span className="switch-knob" aria-hidden="true" />
      </button>
    </div>
  )
}
