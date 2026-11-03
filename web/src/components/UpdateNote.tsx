import { useEffect, useId, useState } from 'react'
import { dismissUpdateNote, getUpdateNote, type UpdateNote as Note } from '../api/server'
import { useOpenSettings } from '../shell/AddonsContext'

/** "Hearthkeep was updated": what the upgrade left for the owner, once, until "Got it". */
export function UpdateNote() {
  const titleId = useId()
  const itemId = useId()
  const openSettings = useOpenSettings()
  const [note, setNote] = useState<Note | null>(null)
  const [closing, setClosing] = useState(false)

  useEffect(() => {
    let live = true
    getUpdateNote()
      .then((r) => live && setNote(r.note))
      .catch(() => {
        // No note is better than an error about a note.
      })
    return () => {
      live = false
    }
  }, [])

  async function dismiss() {
    if (closing) return
    setClosing(true)
    try {
      await dismissUpdateNote()
    } catch {
      // Hidden either way; the note comes back next time if the server didn't hear.
    }
    setNote(null)
  }

  if (!note) return null
  return (
    <section className="update-note" aria-labelledby={titleId}>
      <h2 id={titleId}>Hearthkeep was updated</h2>
      {note.items.length === 0 ? (
        <p>Nothing needs you.</p>
      ) : (
        <ul>
          {note.items.map(({ id, text, addon }) => (
            <li key={id}>
              <p id={`${itemId}-${id}`}>{text}</p>
              {addon && (
                <button
                  type="button"
                  className="btn ghost"
                  aria-describedby={`${itemId}-${id}`}
                  onClick={() => openSettings(`addon-${addon}`)}
                >
                  Open Settings › Add-ons
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="settings-actions">
        <button type="button" className="btn" aria-busy={closing || undefined} onClick={() => void dismiss()}>
          Got it
        </button>
      </div>
    </section>
  )
}
