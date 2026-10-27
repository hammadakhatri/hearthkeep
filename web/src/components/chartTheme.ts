/**
 * The charts' dark theme, read from the design tokens (`tokens.css` custom properties) when a
 * chart is drawn, and handed to vega-embed as its `config`. Client-side only: chart specs from
 * the server never carry a config of their own.
 */

/** Used when a token can't be read (the stylesheet isn't loaded, as in tests). */
const FALLBACK: Record<string, string> = {
  '--s1': '#0e0e10',
  '--s3': '#1f1f22',
  '--line': '#2a2a2e',
  '--fg': '#f5f5f7',
  '--fg2': '#c7c7cc',
  '--muted': '#86868b',
  '--accent': '#2997ff',
  '--amber': '#ffb340',
  '--green': '#32d74b',
  '--red': '#ff6961',
  '--font': "'Geist Sans', system-ui, sans-serif",
}

function token(styles: CSSStyleDeclaration | null, name: string): string {
  return styles?.getPropertyValue(name).trim() || FALLBACK[name]
}

export function chartTheme(): Record<string, unknown> {
  const styles = typeof document === 'undefined' ? null : getComputedStyle(document.documentElement)
  const t = (name: string) => token(styles, name)
  const [bg, grid, line, fg, fg2, muted, font] = [
    t('--s1'),
    t('--s3'),
    t('--line'),
    t('--fg'),
    t('--fg2'),
    t('--muted'),
    t('--font'),
  ]
  const series = [t('--accent'), t('--amber'), t('--green'), t('--red'), '#bf5af2', '#64d2ff', '#ff9f0a', '#a2845e']
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
