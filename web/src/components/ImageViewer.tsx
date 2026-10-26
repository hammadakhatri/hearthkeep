import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { BackIcon, CloseIcon, CopyIcon, ForwardIcon } from '../shell/icons'
import { aspectOf, imageUrl, type GridImage } from './ImageGrid'

/** An image with whatever is known about how it was made (the Images view knows; chat does not). */
export type ViewerImage = GridImage & {
  prompt?: string | null
  model?: string | null
  createdAt?: string | null
}

const FOCUSABLE = 'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
const COPIED_MS = 2000

function when(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/**
 * The full-size image viewer shared by Chat and Images: a modal dialog that keeps focus
 * inside, closes on Escape, moves with the arrow keys and gives focus back to `opener`.
 */
export function ImageViewer({
  images,
  index,
  onIndex,
  onClose,
  opener,
}: {
  images: ViewerImage[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
  opener: HTMLElement | null
}) {
  const dialog = useRef<HTMLDivElement>(null)
  const closeButton = useRef<HTMLButtonElement>(null)
  const returnTo = useRef<HTMLElement | null>(opener)
  const titleId = useId()
  const image = images[Math.min(index, images.length - 1)]
  const last = images.length - 1

  // Focus moves in on open and back to the tile that opened it on close.
  useEffect(() => {
    if (!returnTo.current) returnTo.current = document.activeElement as HTMLElement | null
    closeButton.current?.focus()
    return () => returnTo.current?.focus?.()
  }, [])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onClose()
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault()
        if (index > 0) onIndex(index - 1)
      } else if (event.key === 'ArrowRight') {
        event.preventDefault()
        if (index < last) onIndex(index + 1)
      } else if (event.key === 'Tab') {
        const items = [...(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
        if (!items.length) return
        const active = document.activeElement
        const first = items[0]
        const end = items[items.length - 1]
        const inside = dialog.current?.contains(active) ?? false
        if (event.shiftKey && (active === first || !inside)) {
          event.preventDefault()
          end.focus()
        } else if (!event.shiftKey && (active === end || !inside)) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [index, last, onClose, onIndex])

  if (!image) return null
  const size = image.width && image.height ? `${image.width} × ${image.height}` : null
  const date = image.createdAt ? when(image.createdAt) : ''
  const hasMeta = Boolean(image.prompt || image.model || size || date)

  return createPortal(
    <div className="viewer-scrim" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div
        ref={dialog}
        className="viewer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-meta={hasMeta || undefined}
      >
        <div className="viewer-bar">
          <h2 id={titleId} className="viewer-count">
            Image {index + 1} of {images.length}
          </h2>
          <button ref={closeButton} type="button" className="viewer-btn" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className="viewer-stage" onClick={(event) => event.target === event.currentTarget && onClose()}>
          <img
            key={image.id}
            className="viewer-img"
            src={imageUrl(image.id)}
            alt={image.alt}
            width={image.width ?? undefined}
            height={image.height ?? undefined}
            style={{ aspectRatio: aspectOf(image) }}
            decoding="async"
          />
          {images.length > 1 && (
            <>
              <button
                type="button"
                className="viewer-btn viewer-prev"
                aria-label="Previous image"
                disabled={index === 0}
                onClick={() => onIndex(index - 1)}
              >
                <BackIcon />
              </button>
              <button
                type="button"
                className="viewer-btn viewer-next"
                aria-label="Next image"
                disabled={index === last}
                onClick={() => onIndex(index + 1)}
              >
                <ForwardIcon />
              </button>
            </>
          )}
        </div>
        {hasMeta && (
          <div className="viewer-meta">
            {image.prompt && <PromptBlock key={image.id} prompt={image.prompt} />}
            <dl className="specs">
              {image.model && (
                <div>
                  <dt>Model</dt>
                  <dd>{image.model}</dd>
                </div>
              )}
              {size && (
                <div>
                  <dt>Size</dt>
                  <dd>{size}</dd>
                </div>
              )}
              {date && (
                <div>
                  <dt>Created</dt>
                  <dd>{date}</dd>
                </div>
              )}
            </dl>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}

/** The prompt, with Copy prompt: the clipboard when the browser allows it, else the text selected. */
function PromptBlock({ prompt }: { prompt: string }) {
  const text = useRef<HTMLParagraphElement>(null)
  const [note, setNote] = useState('')

  useEffect(() => {
    if (!note) return
    const timer = setTimeout(() => setNote(''), COPIED_MS)
    return () => clearTimeout(timer)
  }, [note])

  async function copy() {
    try {
      await navigator.clipboard.writeText(prompt)
      setNote('Copied')
      return
    } catch {
      // No clipboard (an insecure origin or a refusal): select the text instead.
    }
    const el = text.current
    const selection = window.getSelection()
    if (el && selection) {
      const range = document.createRange()
      range.selectNodeContents(el)
      selection.removeAllRanges()
      selection.addRange(range)
    }
    let copied = false
    try {
      copied = document.execCommand('copy')
    } catch {
      copied = false
    }
    setNote(copied ? 'Copied' : 'Prompt selected. Copy it with your keyboard.')
  }

  return (
    <div className="viewer-prompt">
      <p ref={text} className="viewer-prompt-text">
        {prompt}
      </p>
      <div className="viewer-prompt-actions">
        <button type="button" className="btn" onClick={() => void copy()}>
          <CopyIcon />
          Copy prompt
        </button>
        <span className="viewer-copied" role="status">
          {note}
        </span>
      </div>
    </div>
  )
}
