/**
 * The charts' dark theme, read from the design tokens (`tokens.css` custom properties) when a
 * chart is drawn, and handed to vega-embed as its `config`. Client-side only: chart specs from
 * the server never carry a config of their own.
 */

const SERIES = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6', '--chart-7', '--chart-8']

/**
 * The tokens' values, used only when a token can't be read (the stylesheet isn't loaded, as
 * in tests). The one place these colours are repeated outside tokens.css.
 */
const FALLBACK: Record<string, string> = {
  '--s1': '#0e0e10',
  '--s3': '#1f1f22',
  '--line': '#2a2a2e',
  '--fg': '#f5f5f7',
  '--fg2': '#c7c7cc',
  '--muted': '#86868b',
  '--font': "'Geist Sans', system-ui, sans-serif",
  '--chart-1': '#2997ff',
  '--chart-2': '#ffb340',
  '--chart-3': '#32d74b',
  '--chart-4': '#ff6961',
  '--chart-5': '#bf5af2',
  '--chart-6': '#64d2ff',
  '--chart-7': '#ff9f0a',
  '--chart-8': '#ac8e68',
}

export function chartTheme(): Record<string, unknown> {
  const styles = typeof document === 'undefined' ? null : getComputedStyle(document.documentElement)
  const t = (name: string) => styles?.getPropertyValue(name).trim() || FALLBACK[name]
  const [bg, grid, line, fg, fg2, muted, font] = ['--s1', '--s3', '--line', '--fg', '--fg2', '--muted', '--font'].map(t)
  const series = SERIES.map(t)
  const axis = {
    domainColor: line,
    gridColor: grid,
    tickColor: line,
    labelColor: fg2,
    titleColor: muted,
    labelFont: font,
    titleFont: font,
    labelFontSize: 11,
    titleFontSize: 11,
    titleFontWeight: 500,
  }
  return {
    background: bg,
    font,
    padding: 8,
    view: { stroke: 'transparent' },
    title: { color: fg, subtitleColor: muted, font, fontSize: 13, fontWeight: 600, anchor: 'start' },
    axis,
    legend: { labelColor: fg2, titleColor: muted, labelFont: font, titleFont: font, symbolStrokeColor: line },
    header: { labelColor: fg2, titleColor: muted, labelFont: font, titleFont: font },
    mark: { color: series[0] },
    line: { strokeWidth: 2 },
    rule: { color: muted },
    text: { color: fg },
    range: { category: series },
  }
}
