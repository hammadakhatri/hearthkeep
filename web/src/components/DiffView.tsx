import { useState } from 'react'

type Line = { kind: 'add' | 'del' | 'context' | 'hunk' | 'meta'; text: string; old?: number; next?: number }
type FileDiff = { path: string; lines: Line[]; added: number; removed: number }
type Parsed = { files: FileDiff[]; notes: string[] }

const FOLD_OVER = 5
const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/
const GIT_META =
  /^(index |new file mode|deleted file mode|old mode|new mode|similarity index|rename from|rename to|copy from|copy to)/

function stripPrefix(path: string): string {
  return path.replace(/^"?[ab]\//, '').replace(/"$/, '')
}

/** Read a unified diff into files, lines with both line numbers, and loose notes. Text only. */
export function parseDiff(diff: string): Parsed {
  const files: FileDiff[] = []
  const notes: string[] = []
  let file = null as FileDiff | null
  let oldLeft = 0
  let newLeft = 0
  let oldNo = 0
  let newNo = 0

  const start = (path: string): FileDiff => {
    const next: FileDiff = { path, lines: [], added: 0, removed: 0 }
    files.push(next)
    file = next
    oldLeft = newLeft = 0
    return next
  }

  for (const raw of diff.split('\n')) {
    if (file && (oldLeft > 0 || newLeft > 0)) {
      const sign = raw.charAt(0)
      const text = raw.slice(1)
      if (sign === '+') {
        file.lines.push({ kind: 'add', text, next: newNo++ })
        file.added++
        newLeft--
        continue
      }
      if (sign === '-') {
        file.lines.push({ kind: 'del', text, old: oldNo++ })
        file.removed++
        oldLeft--
        continue
      }
      if (sign === ' ') {
        file.lines.push({ kind: 'context', text, old: oldNo++, next: newNo++ })
        oldLeft--
        newLeft--
        continue
      }
      if (sign === '\\') {
        file.lines.push({ kind: 'meta', text: raw })
        continue
      }
      // Anything else ends the hunk early (a cut diff): read it normally below.
      oldLeft = newLeft = 0
    }
    if (raw.startsWith('diff --git ')) {
      const rest = raw.slice(11)
      const split = rest.lastIndexOf(' b/')
      const match = /^(.+?) (\S+)$/.exec(rest)
      start(stripPrefix(split > 0 ? rest.slice(split + 1) : match ? match[2] : rest))
      continue
    }
    const hunk = HUNK.exec(raw)
    if (hunk) {
      const current: FileDiff = file ?? start('(changes)')
      oldNo = Number(hunk[1])
      newNo = Number(hunk[3])
      oldLeft = hunk[2] === undefined ? 1 : Number(hunk[2])
      newLeft = hunk[4] === undefined ? 1 : Number(hunk[4])
      current.lines.push({ kind: 'hunk', text: raw })
      continue
    }
    const open = file as FileDiff | null
    if (raw.startsWith('--- ')) {
      // Without "diff --git" headers, a header after a hunk starts the next file.
      if (!open || open.lines.some((l) => l.kind === 'hunk')) {
        const name = raw.slice(4)
        start(name === '/dev/null' ? '(new file)' : stripPrefix(name))
      }
      continue
    }
    if (raw.startsWith('+++ ') && open) {
      const target = raw.slice(4)
      if (target !== '/dev/null' && open.lines.length === 0) open.path = stripPrefix(target)
      continue
    }
    if (raw === '' || GIT_META.test(raw)) continue
    notes.push(raw)
  }
  return { files, notes }
}

const SIGN = { add: '+', del: '-', context: '', hunk: '', meta: '' } as const

function FileBlock({ file, startOpen }: { file: FileDiff; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen)
  return (
    <section className="diff-file">
      <div className="diff-file-head">
        <h4>
          <button type="button" className="diff-file-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
            {file.path}
          </button>
        </h4>
        <span className="diff-counts" aria-hidden="true">
          <span className="diff-count add">+{file.added}</span>
          <span className="diff-count del">-{file.removed}</span>
        </span>
      </div>
      {open && (
        <div className="diff-body">
          {file.lines.map((line, i) => (
            <div key={i} className="diff-line" data-kind={line.kind}>
              <span className="diff-no" data-side="old" aria-hidden="true">
                {line.old}
              </span>
              <span className="diff-no" data-side="new" aria-hidden="true">
                {line.next}
              </span>
              <span className="diff-sign" aria-hidden="true">
                {SIGN[line.kind]}
              </span>
              <span className="diff-text">{line.text}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/**
 * A unified diff, one block per file: added lines green, removed red, hunk headers dimmed, line
 * numbers for both sides. More than five files fold, unless `alwaysOpen` (approval cards show it all). Every line is rendered as text; nothing in a diff is ever markup.
 */
export function DiffView({ diff, alwaysOpen = false }: { diff: string; alwaysOpen?: boolean }) {
  const { files, notes } = parseDiff(diff)
  const fold = !alwaysOpen && files.length > FOLD_OVER
  return (
    <div className="diff" data-testid="diff-view">
      {files.map((file, i) => (
        <FileBlock key={`${i}:${file.path}`} file={file} startOpen={!fold} />
      ))}
      {notes.map((note, i) => (
        <p key={i} className="diff-note">
          {note}
        </p>
      ))}
    </div>
  )
}
