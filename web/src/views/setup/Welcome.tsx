import { formatBytes, type Catalog } from '../../api/models'
import { diskLine, SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

/** What the recommended chat model and, when none is there yet, its engine take on disk. */
function recommendedDisk(catalog: Catalog): string | null {
  const chat = catalog.items.find((i) => i.id === catalog.recommended?.chat)
  if (!chat) return null
  const engine = catalog.runtime_ready ? undefined : catalog.items.find((i) => i.id === catalog.recommended?.runtime)
  const bytes = chat.size_bytes + (engine && !engine.installed ? engine.size_bytes : 0)
  return diskLine(formatBytes(bytes), !!engine && !engine.installed)
}

type Props = { catalog: Catalog | null; focus: boolean; onNext(): void }

/** Step 1: what Hearthkeep is, what setup does, how long it takes and the disk it needs. */
export function Welcome({ catalog, focus, onNext }: Props) {
  const disk = catalog ? recommendedDisk(catalog) : null
  return (
    <StepFrame title={SETUP_COPY.welcomeTitle} focus={focus} nextLabel="Next" onNext={onNext}>
      <p className="setup-lede">{SETUP_COPY.welcomeAbout}</p>
      <p className="setup-text">{SETUP_COPY.welcomePlan}</p>
      <p className="setup-text">
        {SETUP_COPY.welcomeTime}
        {disk && ` ${disk}`}
      </p>
    </StepFrame>
  )
}
