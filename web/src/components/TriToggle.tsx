import { useRef, type KeyboardEvent } from 'react'

export type Level = 'allowed' | 'ask' | 'off'

export const LEVELS: { value: Level; label: string }[] = [
  { value: 'allowed', label: 'Allowed' },
  { value: 'ask', label: 'Ask' },
  { value: 'off', label: 'Off' },
]

type Props = {
  /** Accessible name of the group, usually the tool's name. */
  label: string
  value: Level
  onChange: (level: Level) => void
  disabled?: boolean
}

/** Allowed / Ask / Off as one radio group: arrow keys move between levels. */
export function TriToggle({ label, value, onChange, disabled = false }: Props) {
  const group = useRef<HTMLDivElement>(null)

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key]
    if (step === undefined || disabled) return
    event.preventDefault()
    const at = LEVELS.findIndex((l) => l.value === value)
    const next = LEVELS[(at + step + LEVELS.length) % LEVELS.length]
    onChange(next.value)
    group.current?.querySelector<HTMLButtonElement>(`[data-level="${next.value}"]`)?.focus()
  }

  return (
    <div ref={group} className="tri" role="radiogroup" aria-label={label} aria-disabled={disabled || undefined} onKeyDown={onKeyDown}>
      {LEVELS.map((level) => {
        const checked = level.value === value
        return (
          <button
            key={level.value}
            type="button"
            role="radio"
            data-level={level.value}
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            disabled={disabled}
            onClick={() => !checked && onChange(level.value)}
          >
            {level.label}
          </button>
        )
      })}
    </div>
  )
}
