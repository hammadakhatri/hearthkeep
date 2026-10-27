/**
 * Chart tooltips that are text only. Vega calls the handler with whatever the chart's tooltip
 * encoding produced, which comes from the data: every key and value is set as text
 * (`textContent`), never parsed as HTML, so markup in data shows as the characters it is. An
 * `image` entry (vega-tooltip would draw it as an <img>) is dropped, and `title` becomes the
 * tooltip's heading. One floating box per chart, removed with the chart.
 */

const MAX_ROWS = 24
const MAX_TEXT = 200
const GAP = 12

/** A Vega tooltip handler: `(handler, event, item, value)`. */
export type TooltipFn = (handler: unknown, event: MouseEvent, item: unknown, value: unknown) => void

function text(value: unknown): string {
  let out: string
  if (value === null || value === undefined) out = ''
  else if (typeof value === 'number') out = Number.isFinite(value) ? value.toLocaleString('en-GB', { maximumFractionDigits: 6 }) : String(value)
  else if (typeof value === 'string') out = value
  else if (value instanceof Date) out = value.toISOString()
  else if (Array.isArray(value)) out = `[${value.map(text).join(', ')}]`
  else if (typeof value === 'object') {
    try {
      out = JSON.stringify(value)
    } catch {
      out = String(value)
    }
  } else out = String(value)
  return out.length > MAX_TEXT ? `${out.slice(0, MAX_TEXT - 1)}…` : out
}

function fill(box: HTMLElement, value: unknown) {
  box.replaceChildren()
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    box.textContent = text(value)
    return
  }
  const entries = Object.entries(value as Record<string, unknown>).filter(([key]) => key !== 'image')
  const title = entries.find(([key]) => key === 'title')
  if (title) {
    const heading = document.createElement('div')
    heading.className = 'chart-tip-title'
    heading.textContent = text(title[1])
    box.append(heading)
  }
  const rows = entries.filter(([key]) => key !== 'title').slice(0, MAX_ROWS)
  if (!rows.length) return
  const table = document.createElement('dl')
  for (const [key, val] of rows) {
    const name = document.createElement('dt')
    name.textContent = text(key)
    const shown = document.createElement('dd')
    shown.textContent = text(val)
    table.append(name, shown)
  }
  box.append(table)
}

function place(box: HTMLElement, event: MouseEvent) {
  const { innerWidth, innerHeight } = window
  const { width, height } = box.getBoundingClientRect()
  let x = event.clientX + GAP
  let y = event.clientY + GAP
  if (x + width > innerWidth - GAP) x = Math.max(GAP, event.clientX - width - GAP)
  if (y + height > innerHeight - GAP) y = Math.max(GAP, event.clientY - height - GAP)
  box.style.left = `${x}px`
  box.style.top = `${y}px`
}

/**
 * `chart` is the chart's element: on a touch screen there is no "mouse left", so a tap
 * anywhere outside it, or any scroll, hides the tooltip.
 */
export function textTooltip(chart?: HTMLElement): { handler: TooltipFn; destroy: () => void } {
  let box: HTMLDivElement | null = null
  const hide = () => box?.setAttribute('hidden', '')
  const onPointerDown = (event: Event) => {
    if (!(chart && event.target instanceof Node && chart.contains(event.target))) hide()
  }
  const listen = (on: boolean) => {
    const method = on ? 'addEventListener' : 'removeEventListener'
    document[method]('pointerdown', onPointerDown, true)
    document[method]('scroll', hide, true)
    window[method]('resize', hide)
  }
  const handler: TooltipFn = (_handler, event, _item, value) => {
    if (value === null || value === undefined || value === '') {
      hide()
      return
    }
    if (!box) {
      box = document.createElement('div')
      box.className = 'chart-tip'
      box.setAttribute('role', 'tooltip')
      document.body.append(box)
      listen(true)
    }
    fill(box, value)
    box.removeAttribute('hidden')
    place(box, event)
  }
  const destroy = () => {
    if (box) listen(false)
    box?.remove()
    box = null
  }
  return { handler, destroy }
}
