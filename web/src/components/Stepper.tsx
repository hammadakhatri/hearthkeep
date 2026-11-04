type Props = {
  steps: readonly string[]
  /** The index of the step on screen. */
  current: number
  label?: string
}

/** Where the owner is in a run of steps: an ordered list, the current step marked for screen
 *  readers with `aria-current="step"`. At phone width only the current step's name shows; the
 *  others keep theirs for screen readers. */
export function Stepper({ steps, current, label = 'Steps' }: Props) {
  return (
    <ol className="stepper" aria-label={label}>
      {steps.map((name, i) => (
        <li
          key={name}
          className={i < current ? 'done' : i === current ? 'current' : undefined}
          aria-current={i === current ? 'step' : undefined}
        >
          <span className="stepper-num" aria-hidden="true">
            {i + 1}
          </span>
          <span className="stepper-name">{name}</span>
        </li>
      ))}
    </ol>
  )
}
