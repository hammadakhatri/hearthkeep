import { useCallback, useRef, useState, type ReactNode } from 'react'
import { api, isApiError } from '../api/client'
import { AlertIcon, CloseIcon } from '../shell/icons'

/** The photo types the server takes (it checks the bytes, too). */
export const PHOTO_ACCEPT = 'image/png,image/jpeg,image/webp'

/** A message line that carries a photo. Only lines matching exactly are photos. */
export const ATTACHED_PHOTO = /^Attached photo: ([A-Za-z0-9_-]{1,32})$/

export function attachedPhotoLine(id: string): string {
  return `Attached photo: ${id}`
}

/** Same origin, sent with the session cookie: where an uploaded photo is shown from. */
export function uploadUrl(id: string): string {
  return `/api/uploads/${encodeURIComponent(id)}/file`
}

/** A message's text split into the photos it carries and the words the owner wrote. */
export function splitPhotos(text: string): { photos: string[]; text: string } {
  const photos: string[] = []
  const kept = text.split('\n').filter((line) => {
    const match = ATTACHED_PHOTO.exec(line)
    if (match) photos.push(match[1])
    return !match
  })
  return { photos, text: kept.join('\n').trim() }
}

/** An uploaded photo as the server describes it. */
export type Uploaded = { id: string; width: number; height: number }

export type Photo =
  | { state: 'none' }
  | { state: 'uploading' }
  | ({ state: 'ready' } & Uploaded)
  | { state: 'failed'; message: string }

function uploadError(error: unknown): string {
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Your photo wasn't uploaded."
  if (isApiError(error)) {
    const message = (error.body as { message?: unknown } | null)?.message
    if (typeof message === 'string' && message) return message
  }
  return "Your photo couldn't be uploaded. Try again."
}

/**
 * One photo, uploaded as soon as it is picked. A newer pick or a clear wins over a slow upload.
 * `announcement` is what the live region says: "Photo attached" or "Photo removed".
 */
export function usePhoto() {
  const [photo, setPhoto] = useState<Photo>({ state: 'none' })
  const [announcement, setAnnouncement] = useState('')
  const latest = useRef(0)

  /** Resolves with the upload once it is ready, or null if it failed or was replaced. */
  const pick = useCallback(async (file: File): Promise<Uploaded | null> => {
    const token = ++latest.current
    setPhoto({ state: 'uploading' })
    setAnnouncement('')
    const form = new FormData()
    form.append('file', file)
    try {
      const up = await api<Uploaded>('/api/uploads', { method: 'POST', body: form })
      if (token !== latest.current) return null
      setPhoto({ state: 'ready', id: up.id, width: up.width, height: up.height })
      setAnnouncement('Photo attached')
      return up
    } catch (error) {
      if (token === latest.current) setPhoto({ state: 'failed', message: uploadError(error) })
      return null
    }
  }, [])

  /** Drop the photo quietly: it was sent, or the owner moved to another chat. */
  const clear = useCallback(() => {
    latest.current += 1
    setPhoto({ state: 'none' })
    setAnnouncement('')
  }, [])

  /** The owner took the photo away. */
  const remove = useCallback(() => {
    latest.current += 1
    setPhoto({ state: 'none' })
    setAnnouncement('Photo removed')
  }, [])

  return { photo, announcement, pick, clear, remove }
}

/** A button that opens the file picker for one photo. */
export function PhotoButton({
  onPick,
  disabled,
  className,
  label,
  describedBy,
  children,
}: {
  onPick: (file: File) => void
  disabled?: boolean
  className: string
  label?: string
  describedBy?: string
  children: ReactNode
}) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <>
      <button
        type="button"
        className={className}
        aria-label={label}
        aria-describedby={describedBy}
        disabled={disabled}
        onClick={() => input.current?.click()}
      >
        {children}
      </button>
      <input
        ref={input}
        type="file"
        accept={PHOTO_ACCEPT}
        hidden
        tabIndex={-1}
        onChange={(event) => {
          const file = event.target.files?.[0]
          // Cleared so picking the same file again still counts as a change.
          event.target.value = ''
          if (file) onPick(file)
        }}
      />
    </>
  )
}

/**
 * The picked photo while it uploads and once it is ready, with Remove; or why it failed. The
 * live region is always mounted (and visually hidden) so its changes are announced.
 */
export function PhotoChip({ attach }: { attach: ReturnType<typeof usePhoto> }) {
  const { photo, announcement, remove, clear } = attach
  return (
    <>
      <span className="sr-only" role="status" data-testid="photo-status">
        {announcement}
      </span>
      {photo.state === 'failed' ? (
        <div className="note error photo-error" role="alert">
          <AlertIcon />
          <span>{photo.message}</span>
          <button type="button" className="btn ghost" onClick={clear}>
            Dismiss
          </button>
        </div>
      ) : photo.state !== 'none' ? (
        <div className="photo-chip">
          {photo.state === 'ready' ? (
            <img src={uploadUrl(photo.id)} alt="Your photo" width={36} height={36} />
          ) : (
            <span className="photo-chip-wait" aria-hidden="true" />
          )}
          <span className="photo-chip-text">{photo.state === 'ready' ? 'Photo attached' : 'Uploading your photo…'}</span>
          <button type="button" className="iconbtn photo-chip-remove" aria-label="Remove photo" onClick={remove}>
            <CloseIcon />
          </button>
        </div>
      ) : null}
    </>
  )
}
