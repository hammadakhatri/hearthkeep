import qrcode from 'qrcode-generator'
import { useMemo } from 'react'

/** The quiet zone around the code, in modules (the QR spec asks for four). */
const MARGIN = 4

/**
 * A QR code drawn as SVG squares from the encoder's `isDark` grid: no markup strings, so
 * nothing is ever parsed as HTML. Dark modules on a white square, so phone cameras read it on
 * the dark interface.
 */
export function QrCode({ text, size = 176 }: { text: string; size?: number }) {
  const { count, cells } = useMemo(() => {
    const code = qrcode(0, 'M')
    code.addData(text)
    code.make()
    const n = code.getModuleCount()
    const dark: [number, number][] = []
    for (let row = 0; row < n; row++) {
      for (let col = 0; col < n; col++) if (code.isDark(row, col)) dark.push([col, row])
    }
    return { count: n, cells: dark }
  }, [text])
  const extent = count + MARGIN * 2
  return (
    <svg
      className="qr"
      role="img"
      aria-label="QR code for the setup link"
      width={size}
      height={size}
      viewBox={`0 0 ${extent} ${extent}`}
      shapeRendering="crispEdges"
    >
      <rect width={extent} height={extent} fill="#ffffff" />
      {cells.map(([x, y]) => (
        <rect key={`${x}-${y}`} x={x + MARGIN} y={y + MARGIN} width={1} height={1} fill="#000000" />
      ))}
    </svg>
  )
}
