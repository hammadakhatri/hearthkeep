/**
 * A small, safe Markdown renderer for assistant replies. It builds React elements only (no
 * HTML strings), so model text can never inject markup. Supported: paragraphs, headings (shown
 * as bold lines), **bold**, *italic*, `inline code`, fenced code blocks, bullet and numbered
 * lists, and http(s)/mailto links that open in a new tab.
 *
 * With `marks` (the server's number check), each checked number is shown as a number mark.
 * The text is pre-marked with private-use sentinels (OPEN, the mark's index, the number,
 * CLOSE) before parsing, so a number keeps its mark inside bold, lists and link labels; in
 * code, link targets and bare URLs the sentinels are removed and the number stays plain.
 * Sentinel look-alikes already in the text are replaced first, so text cannot forge a mark.
 */

import type { ReactNode } from 'react'
import type { NumberMark } from '../api/runs'

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

const OPEN = '\uE000'
const CLOSE = '\uE001'
const INDEX_BASE = 0xe100
const INDEX_LAST = 0xf8ff
/** Every character the pre-marking uses. */
const SENTINEL = /[\uE000\uE001\uE100-\uF8FF]/g
const MARKED = /\uE000([\uE100-\uF8FF])([^\uE000\uE001]*)\uE001/g

const ORDINAL = /^\d{1,9}$/
const LIST_DOT = /^[.)][ \t]/

/** The text from the start of its line up to `at`. */
function lineBefore(text: string, at: number): string {
  return text.slice(text.lastIndexOf('\n', at - 1) + 1, at)
}

function strip(text: string): string {
  return text.replace(SENTINEL, '')
}

/**
 * The text with each usable mark wrapped in sentinels, and those marks in index order. A mark
 * is used only when its UTF-16 span holds exactly its text and it overlaps no earlier mark.
 */
function premark(text: string, marks: readonly NumberMark[]): { text: string; marks: NumberMark[] } {
  const clean = text.replace(SENTINEL, '\uFFFD')
  const used: NumberMark[] = []
  let out = ''
  let last = 0
  for (const mark of [...marks].sort((a, b) => a.start - b.start)) {
    const { start, end } = mark
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < last || end <= start) continue
    if (end > text.length || text.slice(start, end) !== mark.text) continue
    // A numbered list's own number is drawn as the list marker, not as text.
    if (ORDINAL.test(mark.text) && LIST_DOT.test(text.slice(end)) && !lineBefore(text, start).trim()) continue
    if (INDEX_BASE + used.length > INDEX_LAST) break
    out += clean.slice(last, start) + OPEN + String.fromCharCode(INDEX_BASE + used.length)
    out += clean.slice(start, end) + CLOSE
    used.push(mark)
    last = end
  }
  return { text: out + clean.slice(last), marks: used }
}

/** What a mark says about its number, for the tooltip and screen readers. */
export function markDescription(mark: NumberMark): string {
  switch (mark.kind) {
    case 'computed':
      return 'Computed by code that ran on your file'
    case 'document':
      return mark.page === null ? 'read from the document, not computed' : `read from page ${mark.page}, not computed`
    case 'unverified':
      return mark.note ? `Unverified: ${mark.note}` : 'Unverified'
  }
}

function NumberMarkSpan({ mark, text }: { mark: NumberMark; text: string }) {
  const description = markDescription(mark)
  return (
    <span className={`num num-${mark.kind}`} title={description}>
      {text}
      <span className="sr-only num-note">{description}</span>
    </span>
  )
}

type Marks = readonly NumberMark[] | null

/** Plain text, with its pre-marked numbers as number marks (stray sentinels dropped). */
function withMarks(text: string, key: string, marks: Marks): ReactNode[] {
  if (!marks) return [text]
  const out: ReactNode[] = []
  let last = 0
  let n = 0
  for (const m of text.matchAll(MARKED)) {
    const at = m.index ?? 0
    if (at > last) out.push(strip(text.slice(last, at)))
    const mark = marks[m[1].charCodeAt(0) - INDEX_BASE]
    const body = strip(m[2])
    out.push(mark ? <NumberMarkSpan key={`${key}.n${n++}`} mark={mark} text={body} /> : body)
    last = at + m[0].length
  }
  if (last < text.length) out.push(strip(text.slice(last)))
  return out
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

/**
 * Inline marks. Inside a link label `links` is false: no nested links or auto-linked URLs.
 * `marks` is set when the text was pre-marked.
 */
function inline(text: string, key: string, marks: Marks, links = true): ReactNode[] {
  const out: ReactNode[] = []
  const plain = (part: string) => (marks ? strip(part) : part)
  let last = 0
  let n = 0
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0
    if (at > last) out.push(...withMarks(text.slice(last, at), `${key}.t${n}`, marks))
    const k = `${key}.${n++}`
    if (m[1] !== undefined) {
      const code = plain(m[2])
      out.push(<code key={k}>{code.trim() || code}</code>)
    } else if (m[3] !== undefined) {
      const href = links ? safeHref(plain(m[4])) : null
      out.push(href ? link(href, inline(m[3], k, marks, false), k) : plain(m[0]))
    } else if (m[5] !== undefined || m[6] !== undefined) {
      out.push(<strong key={k}>{inline(m[5] ?? m[6], k, marks, links)}</strong>)
    } else if (m[7] !== undefined || m[8] !== undefined) {
      out.push(<em key={k}>{inline(m[7] ?? m[8], k, marks, links)}</em>)
    } else {
      const url = plain(m[9])
      const href = links ? safeHref(url) : null
      out.push(href ? link(href, url, k) : url)
    }
    last = at + m[0].length
  }
  if (last < text.length) out.push(...withMarks(text.slice(last), `${key}.t${n}`, marks))
  return out
}

function lines(parts: string[], key: string, marks: Marks): ReactNode[] {
  return parts.flatMap((line, i) =>
    i === 0
      ? inline(line, `${key}.${i}`, marks)
      : [<br key={`${key}.br${i}`} />, ...inline(line, `${key}.${i}`, marks)],
  )
}

export function Markdown({ text, marks }: { text: string; marks?: readonly NumberMark[] }) {
  const pre = marks ? premark(text, marks) : null
  const used: Marks = pre ? pre.marks : null
  return (
    <>
      {parseBlocks(pre ? pre.text : text).map((block, b) => {
        const key = String(b)
        switch (block.kind) {
          case 'p':
            return <p key={key}>{lines(block.lines, key, used)}</p>
          case 'h':
            return (
              <p key={key} className="md-heading">
                <strong>{inline(block.text, key, used)}</strong>
              </p>
            )
          case 'code':
            return (
              <pre key={key} className="md-code" data-lang={block.lang || undefined}>
                <code>{used ? strip(block.text) : block.text}</code>
              </pre>
            )
          case 'ul':
            return (
              <ul key={key}>
                {block.items.map((item, i) => (
                  <li key={i}>{inline(item, `${key}.${i}`, used)}</li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={key} start={block.start === 1 ? undefined : block.start}>
                {block.items.map((item, i) => (
                  <li key={i}>{inline(item, `${key}.${i}`, used)}</li>
                ))}
              </ol>
            )
        }
      })}
    </>
  )
}
