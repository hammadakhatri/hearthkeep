import { useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { PhotoButton, type Photo } from '../components/Photo'
import { PaperclipIcon, SendIcon, ThinkIcon } from './icons'

type Props = {
  /** Resolves true when the message was accepted (the box then clears). */
  onSend: (text: string) => Promise<boolean>
  /** A reply is still being written: typing is fine, sending waits. */
  busy: boolean
  think: boolean
  onThinkChange: (think: boolean) => void
  /** Thinking is chosen when a chat starts; an ongoing chat keeps its choice. */
  thinkLocked: boolean
  model: string | null
  /** The photo for the next message, when attaching one is offered. */
  photo?: Photo
  onPickPhoto?: (file: File) => void
}

const MAX_HEIGHT = 200

/** The floating message box: text, a photo, Think, the model in use, Send. */
export function Composer({ onSend, busy, think, onThinkChange, thinkLocked, model, photo, onPickPhoto }: Props) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)
  // A ready photo can go on its own; one still uploading holds the message back.
  const hasContent = text.trim() !== '' || photo?.state === 'ready'
  const canSend = hasContent && photo?.state !== 'uploading' && !busy && !sending

  // Grow with the text, up to a few lines, then scroll inside.
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
  }, [text])

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    if (!canSend) return
    setSending(true)
    const sent = text
    const accepted = await onSend(sent.trim())
    setSending(false)
    // Clear only what was sent: anything typed while it was sending stays.
    if (accepted) setText((now) => (now === sent ? '' : now))
    box.current?.focus()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter adds a line; never while an IME is composing.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submit()
    }
  }

  const thinkTitle = thinkLocked
    ? `Thinking is ${think ? 'on' : 'off'} for this chat. Start a new chat to change it.`
    : 'Let the model think before it answers. Slower, often better for hard questions.'

  return (
    <form className="composer" onSubmit={submit} aria-label="Message">
      <label className="sr-only" htmlFor="composer-input">
        Message Hearthkeep
      </label>
      <textarea
        id="composer-input"
        ref={box}
        className="composer-input"
        rows={1}
        value={text}
        placeholder="Ask Hearthkeep anything…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        maxLength={20000}
        enterKeyHint="send"
        aria-describedby={busy ? 'composer-busy' : undefined}
      />
      {busy && (
        <span id="composer-busy" className="sr-only">
          Hearthkeep is answering. You can send when it's done.
        </span>
      )}
      <div className="composer-tools">
        {onPickPhoto && (
          <PhotoButton className="chipbtn opt attach" label="Attach a photo" onPick={onPickPhoto}>
            <PaperclipIcon />
          </PhotoButton>
        )}
        <button
          type="button"
          className="chipbtn opt"
          aria-pressed={think}
          disabled={thinkLocked}
          title={thinkTitle}
          onClick={() => onThinkChange(!think)}
        >
          <ThinkIcon />
          Think
        </button>
        {model && (
          <span className="chip opt model" title={`Answers come from ${model} on your computer`}>
            {model}
          </span>
        )}
        <button type="submit" className="go" aria-label="Send" disabled={!canSend}>
          <SendIcon />
        </button>
      </div>
    </form>
  )
}
