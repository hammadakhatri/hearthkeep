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

export type Photo =
  | { state: 'none' }
  | { state: 'uploading' }
  | { state: 'ready'; id: string }
  | { state: 'failed'; message: string }

function uploadError(error: unknown): string {
  if (isApiError(error, 'network')) return "Can't reach Hearthkeep. Your photo wasn't uploaded."
  if (isApiError(error)) {
    const message = (error.body as { message?: unknown } | null)?.message
    if (typeof message === 'string' && message) return message
  }
  return "Your photo couldn't be uploaded. Try again."
}

/** One photo, uploaded as soon as it is picked. A newer pick or a clear wins over a slow upload. */
export function usePhoto() {
  const [photo, setPhoto] = useState<Photo>({ state: 'none' })
  const latest = useRef(0)

  const pick = useCallback(async (file: File) => {
    const token = ++latest.current
    setPhoto({ state: 'uploading' })
    const form = new FormData()
    form.append('file', file)
    try {
      const { id } = await api<{ id: string }>('/api/uploads', { method: 'POST', body: form })
      if (token === latest.current) setPhoto({ state: 'ready', id })
    } catch (error) {
      if (token === latest.current) setPhoto({ state: 'failed', message: uploadError(error) })
    }
  }, [])

  const clear = useCallback(() => {
    latest.current += 1
    setPhoto({ state: 'none' })
  }, [])

  return { photo, pick, clear }
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

/** The picked photo while it uploads and once it is ready, with Remove; or why it failed. */
export function PhotoChip({ photo, onRemove }: { photo: Photo; onRemove: () => void }) {
  if (photo.state === 'none') return null
  if (photo.state === 'failed') {
    return (
      <div className="note error photo-error" role="alert">
        <AlertIcon />
        <span>{photo.message}</span>
        <button type="button" className="btn ghost" onClick={onRemove}>
          Dismiss
        </button>
      </div>
    )
  }
  const label = photo.state === 'ready' ? 'Photo attached' : 'Uploading your photo'
  return (
    <div className="photo-chip" role="status" aria-label={label}>
      {photo.state === 'ready' ? (
        <img src={uploadUrl(photo.id)} alt="Your photo" width={36} height={36} />
      ) : (
        <span className="photo-chip-wait" aria-hidden="true" />
      )}
      <span className="photo-chip-text">{photo.state === 'ready' ? 'Photo attached' : 'Uploading your photo…'}</span>
      <button type="button" className="iconbtn photo-chip-remove" aria-label="Remove photo" onClick={onRemove}>
        <CloseIcon />
      </button>
    </div>
  )
}
