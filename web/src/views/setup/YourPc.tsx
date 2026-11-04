import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import type { SetupPc } from '../../api/setup'
import { AlertIcon } from '../../shell/icons'
import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

type Props = {
  info: SetupPc | null
  failed: boolean
  onReload(): void
  /** Hide "Change…": this PC can't move folders (not Windows). */
  canMoveHere: boolean
  moving: boolean
  /** Ask the server to move the data folder; the sentence to show when it can't. */
  onMove(path: string): Promise<string | null>
  focus: boolean
  onBack(): void
  onNext(): void
}

function graphics(info: SetupPc): string {
  const { pc } = info
  if (!pc.nvidia) return SETUP_COPY.noNvidia
  const name = pc.gpu_name ?? 'NVIDIA graphics card'
  return pc.vram_gb ? `${name}, ${Math.round(pc.vram_gb)} GB of graphics memory` : name
}

/** Step 2: the graphics card, memory and free space, and where Hearthkeep keeps its data. */
export function YourPc({ info, failed, onReload, canMoveHere, moving, onMove, focus, onBack, onNext }: Props) {
  const hintId = useId()
  const [changing, setChanging] = useState(false)
  const [path, setPath] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const changeRef = useRef<HTMLButtonElement>(null)
  const wasChanging = useRef(false)
  const statusRef = useRef<HTMLParagraphElement>(null)

  // After a successful move the form is gone: focus goes to the status line, not the page.
  useEffect(() => {
    if (moving) statusRef.current?.focus()
  }, [moving])

  // The form opens with the cursor in its field; closing it gives focus back to "Change…".
  useEffect(() => {
    if (changing) inputRef.current?.focus()
    else if (wasChanging.current) changeRef.current?.focus()
    wasChanging.current = changing
  }, [changing])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const target = path.trim()
    if (!target || sending) return
    setSending(true)
    setError(null)
    const problem = await onMove(target)
    setSending(false)
    if (problem) setError(problem)
    else setChanging(false)
  }

  const busy = moving || sending
  return (
    <StepFrame title={SETUP_COPY.pcTitle} focus={focus} onBack={onBack} nextLabel="Next" onNext={onNext} busy={busy}>
      {failed && !info && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{SETUP_COPY.pcFailed}</span>
          <button type="button" className="btn ghost" onClick={onReload}>
            Retry
          </button>
        </div>
      )}
      {!info && !failed && (
        <div className="device-list skeleton" aria-busy="true" aria-label="Checking this PC">
          <span className="bar w30" />
          <span className="bar w60" />
        </div>
      )}
      {info && (
        <dl className="setup-facts">
          <div>
            <dt>Graphics</dt>
            <dd>{graphics(info)}</dd>
          </div>
          <div>
            <dt>Memory</dt>
            <dd>{info.pc.ram_gb ? `${Math.round(info.pc.ram_gb)} GB` : SETUP_COPY.notKnown}</dd>
          </div>
          <div>
            <dt>Free space</dt>
            <dd>{`${Math.round(info.pc.free_disk_gb)} GB`}</dd>
          </div>
          <div className="wide">
            <dt>Data folder</dt>
            <dd>
              <span className="folder-path">{info.data_dir}</span>
              {info.data_dir_default && <span className="perm-sub">{SETUP_COPY.defaultFolder}</span>}
            </dd>
          </div>
        </dl>
      )}
      <p ref={statusRef} tabIndex={-1} className="setup-text" role="status" aria-live="polite">
        {moving ? SETUP_COPY.moving : ''}
      </p>
      {info && info.can_move_data && canMoveHere && !moving && !changing && (
        <div className="settings-actions">
          <button
            ref={changeRef}
            type="button"
            className="btn ghost"
            onClick={() => {
              setPath(info.data_dir)
              setError(null)
              setChanging(true)
            }}
          >
            Change…
          </button>
        </div>
      )}
      {info && changing && !moving && (
        <form className="tile setup-move" onSubmit={(e) => void submit(e)} aria-busy={sending || undefined}>
          <label className="field">
            <span>New data folder</span>
            <input
              ref={inputRef}
              className="input"
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              aria-describedby={hintId}
              autoComplete="off"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              maxLength={260}
            />
          </label>
          <p id={hintId} className="hint">
            {SETUP_COPY.moveHint}
          </p>
          {error && (
            <div className="note error" role="alert">
              <AlertIcon />
              <span>{error}</span>
            </div>
          )}
          <div className="settings-actions">
            <button type="submit" className="btn primary" disabled={sending || !path.trim()} aria-busy={sending || undefined}>
              Move here
            </button>
            <button type="button" className="btn ghost" disabled={sending} onClick={() => setChanging(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </StepFrame>
  )
}
