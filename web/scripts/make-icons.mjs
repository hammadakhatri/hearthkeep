// Draws the Hearthkeep mark (a rounded square with a blue gradient and a point of light)
// as public/icon-192.png and public/icon-512.png. No dependencies: run `node scripts/make-icons.mjs`.

import { writeFileSync } from 'node:fs'
import { deflateSync } from 'node:zlib'

const TOP = [0x29, 0x97, 0xff] // #2997ff
const BOTTOM = [0x0a, 0x3d, 0x7a] // #0a3d7a

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  return c >>> 0
})

function crc32(buf) {
  let c = 0xffffffff
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([len, body, crc])
}

const clamp01 = (v) => Math.min(1, Math.max(0, v))

/** Signed distance from (x, y) to a rounded square of half-size h and corner radius r. */
function roundedSquare(x, y, h, r) {
  const qx = Math.abs(x) - (h - r)
  const qy = Math.abs(y) - (h - r)
  const outside = Math.hypot(Math.max(qx, 0), Math.max(qy, 0))
  return outside + Math.min(Math.max(qx, qy), 0) - r
}

function draw(size) {
  const px = Buffer.alloc(size * size * 4)
  const half = size / 2
  const radius = size * 0.225 // continuous-corner feel, like the 24 px mark's 7 px radius
  const light = { x: size * 0.3, y: size * 0.25, r: size * 0.08 }
  // Gradient along 140 degrees (CSS): from the top-left towards the bottom-right.
  const angle = ((140 - 90) * Math.PI) / 180
  const dx = Math.cos(angle)
  const dy = Math.sin(angle)
  const span = Math.abs(size * dx) + Math.abs(size * dy)

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const cx = x + 0.5
      const cy = y + 0.5
      const t = clamp01(((cx - half) * dx + (cy - half) * dy) / span + 0.5)
      let rgb = TOP.map((a, i) => a + (BOTTOM[i] - a) * t)
      // Point of light, antialiased over one pixel.
      const glow = clamp01(light.r - Math.hypot(cx - light.x, cy - light.y) + 0.5)
      rgb = rgb.map((c) => c + (255 - c) * glow)
      const alpha = clamp01(0.5 - roundedSquare(cx - half, cy - half, half, radius))
      const o = (y * size + x) * 4
      px[o] = Math.round(rgb[0])
      px[o + 1] = Math.round(rgb[1])
      px[o + 2] = Math.round(rgb[2])
      px[o + 3] = Math.round(alpha * 255)
    }
  }

  const raw = Buffer.alloc(size * (size * 4 + 1))
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0 // filter: none
    px.copy(raw, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4)
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0)
  header.writeUInt32BE(size, 4)
  header[8] = 8 // bit depth
  header[9] = 6 // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

for (const size of [192, 512]) {
  const file = new URL(`../public/icon-${size}.png`, import.meta.url)
  writeFileSync(file, draw(size))
  console.log(`wrote public/icon-${size}.png`)
}
