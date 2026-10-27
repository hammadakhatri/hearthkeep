/**
 * The chart runtime: Vega, Vega-Lite and vega-embed, bundled with the app (no CDN). Loaded
 * only through a dynamic `import()` from ChartView, so it is its own chunk and the chat stays
 * light.
 *
 * Specs are validated on the server (inline values only, no config, no expressions, an
 * allow-listed paint, no image or href). Here they are drawn as SVG with no actions menu,
 * with the expression interpreter instead of compiled code (the app's CSP has no
 * `unsafe-eval`), with a loader that refuses every load, and with text-only tooltips.
 */

import type { Loader, View } from 'vega'
import embed from 'vega-embed'
import { expressionInterpreter } from 'vega-interpreter'
import { chartTheme } from './chartTheme'
import { textTooltip } from './chartTooltip'

function refuse(): Promise<never> {
  return Promise.reject(new Error('Charts never load anything.'))
}

/** A Vega loader that refuses every load, request, file read and link. */
export function noNetworkLoader(): Loader {
  return { load: refuse, http: refuse, file: refuse, sanitize: refuse }
}

export async function embedChart(
  el: HTMLElement,
  spec: object,
  opts: { interactive: boolean },
): Promise<{ view: View; finalize: () => void }> {
  // vega-embed reads embed options (a loader among them) from a spec's `usermeta`, and a
  // spec's own `config` would override the theme. The server refuses both; drop them anyway.
  const { usermeta: _usermeta, config: _config, ...safe } = spec as Record<string, unknown>
  const tip = opts.interactive ? textTooltip() : null
  try {
    const result = await embed(el, safe as never, {
      actions: false,
      renderer: 'svg',
      ast: true,
      expr: expressionInterpreter,
      loader: noNetworkLoader(),
      config: chartTheme(),
      tooltip: tip ? tip.handler : false,
      defaultStyle: false,
    })
    return {
      view: result.view,
      finalize: () => {
        result.finalize()
        tip?.destroy()
      },
    }
  } catch (error) {
    tip?.destroy()
    throw error
  }
}

/** The chart as a PNG at twice its size. Decoded here: the CSP keeps `fetch` off `data:` URLs. */
export async function exportPng(view: View): Promise<Blob> {
  const url = await view.toImageURL('png', 2)
  const comma = url.indexOf(',')
  if (!url.startsWith('data:image/png;base64,') || comma < 0) throw new Error('Not a PNG.')
  const binary = atob(url.slice(comma + 1))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return new Blob([bytes], { type: 'image/png' })
}
