import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expressionInterpreter } from 'vega-interpreter'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { chartTheme } from '../components/chartTheme'
import { textTooltip } from '../components/chartTooltip'
import { ChartView } from '../components/ChartView'
import { embedChart, exportPng, noNetworkLoader } from '../components/vegaRuntime'

const embed = vi.hoisted(() => vi.fn())
vi.mock('vega-embed', () => ({ default: embed }))

// "\x89PNG" as base64.
const PNG_URL = 'data:image/png;base64,iVBORw=='

function fakeView() {
  return { toImageURL: vi.fn(async () => PNG_URL) }
}

const spec = { mark: 'bar', data: { values: [{ a: 1 }] }, encoding: { x: { field: 'a', type: 'quantitative' } } }

beforeEach(() => {
  embed.mockReset()
  embed.mockImplementation(async (el: HTMLElement) => {
    el.innerHTML = '<svg></svg>'
    return { view: fakeView(), finalize: vi.fn() }
  })
})

describe('ChartView', () => {
  it('chart view embeds with the no-network loader and the interpreter', async () => {
    render(<ChartView spec={spec} title="Sales by week" />)
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1))
    const [el, given, opts] = embed.mock.calls[0]
    expect(el).toBeInstanceOf(HTMLElement)
    expect(given).toEqual(spec)
    expect(opts).toMatchObject({ mode: 'vega-lite', actions: false, renderer: 'svg', ast: true, defaultStyle: false })
    expect(opts.expr).toBe(expressionInterpreter)
    expect(typeof opts.tooltip).toBe('function')
    expect(opts.config).toEqual(chartTheme())
    await expect(opts.loader.load('https://x')).rejects.toThrow()
    await expect(opts.loader.http('https://x', {})).rejects.toThrow()
    await expect(opts.loader.file('/etc/passwd')).rejects.toThrow()
    await expect(opts.loader.sanitize('https://x', {})).rejects.toThrow()
    expect(screen.getByRole('figure', { name: 'Sales by week' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Chart: Sales by week' })).toBeInTheDocument()
  })

  it('save as png produces a png blob', async () => {
    const view = fakeView()
    const blob = await exportPng(view as never)
    expect(view.toImageURL).toHaveBeenCalledWith('png', 2)
    expect(blob.type).toBe('image/png')
    expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual([0x89, 0x50, 0x4e, 0x47])

    const created = vi.fn((_: Blob) => 'blob:chart')
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<ChartView spec={spec} title="Sales / week" />)
    const save = screen.getByRole('button', { name: 'Save as PNG' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(created.mock.calls[0][0].type).toBe('image/png')
    expect((click.mock.contexts[0] as HTMLAnchorElement).download).toBe('Sales week.png')
  })

  it('says so when a chart cannot be shown', async () => {
    embed.mockRejectedValueOnce(new Error('bad spec'))
    render(<ChartView spec={spec} title="Broken" />)
    expect(await screen.findByText("This chart couldn't be shown.")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save as PNG' })).toBeNull()
  })

  it('a failed chart tries again when its spec changes', async () => {
    embed.mockRejectedValueOnce(new Error('bad spec'))
    const { rerender } = render(<ChartView spec={spec} title="Retry" />)
    expect(await screen.findByText("This chart couldn't be shown.")).toBeInTheDocument()
    rerender(<ChartView spec={{ ...spec, mark: 'line' }} title="Retry" />)
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText("This chart couldn't be shown.")).toBeNull())
    expect(screen.getByRole('button', { name: 'Save as PNG' })).toBeEnabled()
  })

  it('embeds only once the chart is scrolled into view', async () => {
    const observers: { cb: IntersectionObserverCallback; el?: Element; off: boolean }[] = []
    class FakeObserver {
      rec: { cb: IntersectionObserverCallback; el?: Element; off: boolean }
      constructor(cb: IntersectionObserverCallback) {
        this.rec = { cb, off: false }
        observers.push(this.rec)
      }
      observe(el: Element) {
        this.rec.el = el
      }
      disconnect() {
        this.rec.off = true
      }
      unobserve() {}
      takeRecords() {
        return []
      }
    }
    vi.stubGlobal('IntersectionObserver', FakeObserver)
    render(<ChartView spec={spec} title="Later" />)
    await new Promise((r) => setTimeout(r, 20))
    expect(embed).not.toHaveBeenCalled()
    const [o] = observers
    act(() => o.cb([{ isIntersecting: false, target: o.el! } as never], {} as never))
    await new Promise((r) => setTimeout(r, 20))
    expect(embed).not.toHaveBeenCalled()
    act(() => o.cb([{ isIntersecting: true, target: o.el! } as never], {} as never))
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1))
    expect(o.off).toBe(true)
  })

  it('a static chart has no tooltips', async () => {
    render(<ChartView spec={spec} title="Static" interactive={false} />)
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1))
    expect(embed.mock.calls[0][2].tooltip).toBe(false)
  })

  it('the spec cannot bring its own embed options', async () => {
    const el = document.createElement('div')
    await embedChart(el, { ...spec, usermeta: { embedOptions: { loader: { baseURL: 'https://x' } } } }, { interactive: true })
    expect(embed.mock.calls[0][1]).not.toHaveProperty('usermeta')
  })

  it('the no-network loader refuses everything', async () => {
    const loader = noNetworkLoader()
    await expect(loader.load('data.csv')).rejects.toThrow()
  })
})

describe('chart tooltips', () => {
  it('render text only and drop images', () => {
    const tip = textTooltip()
    const event = new MouseEvent('mousemove', { clientX: 10, clientY: 10 })
    tip.handler({}, event, {}, { image: '/api/x', '<b>a</b>': 1, title: '<i>t</i>' })
    const box = document.querySelector('.chart-tip')!
    expect(box).not.toBeNull()
    expect(box.querySelector('img, b, i')).toBeNull()
    expect(box.textContent).toContain('<b>a</b>')
    expect(box.textContent).toContain('<i>t</i>')
    expect(box.textContent).not.toContain('/api/x')

    tip.handler({}, event, {}, '<img src=x>')
    expect(box.querySelector('img')).toBeNull()
    expect(box.textContent).toBe('<img src=x>')

    tip.handler({}, event, {}, null)
    expect(box).toHaveAttribute('hidden')
    tip.destroy()
    expect(document.querySelector('.chart-tip')).toBeNull()
  })

  it('a tap elsewhere or a scroll hides it', () => {
    const chart = document.createElement('div')
    document.body.append(chart)
    const tip = textTooltip(chart)
    const show = () => tip.handler({}, new MouseEvent('pointermove', { clientX: 5, clientY: 5 }), {}, { a: 1 })
    show()
    const box = document.querySelector('.chart-tip')!
    expect(box).not.toHaveAttribute('hidden')
    // A tap on the chart itself (Vega shows the next tooltip) leaves it be.
    chart.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    expect(box).not.toHaveAttribute('hidden')
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    expect(box).toHaveAttribute('hidden')
    show()
    expect(box).not.toHaveAttribute('hidden')
    document.dispatchEvent(new Event('scroll'))
    expect(box).toHaveAttribute('hidden')
    tip.destroy()
    // Listeners are gone with it.
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    chart.remove()
    expect(document.querySelector('.chart-tip')).toBeNull()
  })
})

describe('chartTheme', () => {
  it('reads the dark colours from the design tokens', () => {
    document.documentElement.style.setProperty('--s1', '#101112')
    document.documentElement.style.setProperty('--fg2', '#abcdef')
    document.documentElement.style.setProperty('--chart-2', '#fedcba')
    const theme = chartTheme() as Record<string, any>
    expect(theme.background).toBe('#101112')
    expect(theme.axis.labelColor).toBe('#abcdef')
    expect(theme.range.category[1]).toBe('#fedcba')
    expect(theme.range.category).toHaveLength(8)
    document.documentElement.style.removeProperty('--chart-2')
    document.documentElement.style.removeProperty('--s1')
    document.documentElement.style.removeProperty('--fg2')
  })
})
