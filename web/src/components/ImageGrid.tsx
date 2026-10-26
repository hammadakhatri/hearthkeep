import { useState } from 'react'

/** One generated image as a grid or the viewer shows it. Width and height, when known, keep its shape. */
export type GridImage = { id: string; alt: string; width?: number | null; height?: number | null }

/** Same origin, sent with the session cookie: the only place an image file comes from. */
export function imageUrl(id: string): string {
  return `/api/images/${encodeURIComponent(id)}/file`
}

/** The CSS aspect ratio of an image, or undefined when its size is not known. */
export function aspectOf(image: { width?: number | null; height?: number | null }): string | undefined {
  return image.width && image.height ? `${image.width} / ${image.height}` : undefined
}

/**
 * A responsive grid of image thumbnails in fixed boxes, so nothing moves while they load.
 * Each tile is a button that opens the image; `onOpen` gets the tile so focus can return to it.
 */
export function ImageGrid({
  images,
  onOpen,
  className = '',
  label,
}: {
  images: GridImage[]
  onOpen: (index: number, tile: HTMLElement) => void
  className?: string
  label?: string
}) {
  if (!images.length) return null
  return (
    <ul className={`imgrid ${className}`.trim()} aria-label={label}>
      {images.map((image, index) => (
        <li key={image.id}>
          <button type="button" className="imgtile" onClick={(event) => onOpen(index, event.currentTarget)}>
            <Thumb image={image} />
          </button>
        </li>
      ))}
    </ul>
  )
}

/** The image itself: lazy, decoded off the main thread, and a quiet blank if the file is gone. */
export function Thumb({ image, eager = false }: { image: GridImage; eager?: boolean }) {
  const [broken, setBroken] = useState(false)
  return (
    <img
      src={imageUrl(image.id)}
      alt={image.alt}
      width={image.width ?? undefined}
      height={image.height ?? undefined}
      loading={eager ? 'eager' : 'lazy'}
      decoding="async"
      data-broken={broken || undefined}
      onError={() => setBroken(true)}
    />
  )
}
