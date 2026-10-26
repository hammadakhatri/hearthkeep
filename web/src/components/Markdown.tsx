/**
 * A small, safe Markdown renderer for assistant replies. It builds React elements only (no
 * HTML strings), so model text can never inject markup. Supported: paragraphs, headings (shown
 * as bold lines), **bold**, *italic*, `inline code`, fenced code blocks, bullet and numbered
 * lists, and http(s)/mailto links that open in a new tab.
 */

import type { ReactNode } from 'react'

const FENCE = /^\s*(```|~~~)\s*([\w+-]*)\s*$/
const BULLET = /^\s*[-*+]\s+(.*)$/
const NUMBERED = /^\s*(\d{1,9})[.)]\s+(.*)$/
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/

type Block =
  | { kind: 'p'; lines: string[] }
  | { kind: 'h'; text: string }
  | { kind: 'code'; lang: string; text: string }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; start: number; items: string[] }

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const fence = FENCE.exec(line)
    if (fence) {
      const body: string[] = []
      i += 1
      // An unclosed fence (still streaming) runs to the end.
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++])
      i += 1
      blocks.push({ kind: 'code', lang: fence[2], text: body.join('\n') })
      continue
    }
    if (!line.trim()) {
      i += 1
      continue
    }
    const heading = HEADING.exec(line)
    if (heading) {
      blocks.push({ kind: 'h', text: heading[1] })
      i += 1
      continue
    }
    if (BULLET.test(line)) {
      const items: string[] = []
      while (i < lines.length && BULLET.test(lines[i])) items.push(BULLET.exec(lines[i++])![1])
      blocks.push({ kind: 'ul', items })
      continue
    }
    const numbered = NUMBERED.exec(line)
    if (numbered) {
      const items: string[] = []
      while (i < lines.length && NUMBERED.test(lines[i])) items.push(NUMBERED.exec(lines[i++])![2])
      blocks.push({ kind: 'ol', start: Number(numbered[1]), items })
      continue
    }
    const para: string[] = []
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE.test(lines[i]) &&
      !HEADING.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !NUMBERED.test(lines[i])
    ) {
      para.push(lines[i++])
    }
    blocks.push({ kind: 'p', lines: para })
  }
  return blocks
}

/** Only absolute web and mail links; everything else stays as plain text. */
export function safeHref(url: string): string | null {
  try {
    const parsed = new URL(url)
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? parsed.href : null
  } catch {
    return null
  }
}

const INLINE =
  /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)|\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*(?=\S)([\s\S]+?)\*\*|__(?=\S)([\s\S]+?)__|\*(?=[^\s*])([^*\n]+?)\*|(?<![\w])_(?=[^\s_])([^_\n]+?)_(?![\w])|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g

function link(href: string, children: ReactNode, key: string) {
  return (
    <a key={key} href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let n = 0
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0
    if (at > last) out.push(text.slice(last, at))
    const k = `${key}.${n++}`
    if (m[1] !== undefined) {
      out.push(<code key={k}>{m[2].trim() || m[2]}</code>)
    } else if (m[3] !== undefined) {
      const href = safeHref(m[4])
      out.push(href ? link(href, inline(m[3], k), k) : m[0])
    } else if (m[5] !== undefined || m[6] !== undefined) {
      out.push(<strong key={k}>{inline(m[5] ?? m[6], k)}</strong>)
    } else if (m[7] !== undefined || m[8] !== undefined) {
      out.push(<em key={k}>{inline(m[7] ?? m[8], k)}</em>)
    } else {
      const href = safeHref(m[9])
      out.push(href ? link(href, m[9], k) : m[9])
    }
    last = at + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function lines(parts: string[], key: string): ReactNode[] {
  return parts.flatMap((line, i) => (i === 0 ? inline(line, `${key}.${i}`) : [<br key={`${key}.br${i}`} />, ...inline(line, `${key}.${i}`)]))
}

export function Markdown({ text }: { text: string }) {
  return (
    <>
      {parseBlocks(text).map((block, b) => {
        const key = String(b)
        switch (block.kind) {
          case 'p':
            return <p key={key}>{lines(block.lines, key)}</p>
          case 'h':
            return (
              <p key={key} className="md-heading">
                <strong>{inline(block.text, key)}</strong>
              </p>
            )
          case 'code':
            return (
              <pre key={key} className="md-code" data-lang={block.lang || undefined}>
                <code>{block.text}</code>
              </pre>
            )
          case 'ul':
            return (
              <ul key={key}>
                {block.items.map((item, i) => (
                  <li key={i}>{inline(item, `${key}.${i}`)}</li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={key} start={block.start === 1 ? undefined : block.start}>
                {block.items.map((item, i) => (
                  <li key={i}>{inline(item, `${key}.${i}`)}</li>
                ))}
              </ol>
            )
        }
      })}
    </>
  )
}
