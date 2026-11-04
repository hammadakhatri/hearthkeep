import type { Catalog, CatalogItem } from '../../api/models'
import { ChatModelPicker, MODELS_ERRORS } from '../../components/ModelsPanel'
import { AlertIcon } from '../../shell/icons'
import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

type Props = {
  catalog: Catalog | null
  failed: boolean
  onReload(): void
  chosen: readonly string[]
  onChoose(item: CatalogItem): void
  onUse(item: CatalogItem): Promise<void>
  onExternal(baseUrl: string, apiKey: string | null, model: string | null): Promise<void>
  busy: boolean
  error: string | null
  /** What the last change did, for the live region. */
  notice: string | null
  focus: boolean
  onBack(): void
  onNext(): void
}

/** Step 3: the chat model, chosen for the one download setup starts later, or a model server the
 *  owner already runs. */
export function ChatModel(props: Props) {
  const { catalog, failed, chosen, busy, error, notice, focus } = props
  const picked =
    catalog !== null &&
    (catalog.chat.source !== 'none' || catalog.items.some((i) => i.kind === 'chat' && chosen.includes(i.id)))
  return (
    <StepFrame
      title={SETUP_COPY.chatTitle}
      focus={focus}
      onBack={props.onBack}
      nextLabel={picked ? 'Next' : 'Skip for now'}
      onNext={props.onNext}
      busy={busy}
    >
      <p className="setup-lede">{SETUP_COPY.chatLede}</p>
      <p className="setup-text" role="status" aria-live="polite">
        {notice ?? ''}
      </p>
      {error && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{error}</span>
        </div>
      )}
      <CatalogState catalog={catalog} failed={failed} onReload={props.onReload} label="Loading chat models" />
      {catalog && (
        <div aria-busy={busy || undefined}>
          <ChatModelPicker
            catalog={catalog}
            action={{ kind: 'choose', chosen, onChoose: props.onChoose }}
            onUse={(i) => void props.onUse(i)}
            external={{
              initialAddress: catalog.chat.source === 'external' ? (catalog.chat.base_url ?? '') : '',
              busy,
              passkey: false,
              onSave: props.onExternal,
            }}
          />
        </div>
      )}
    </StepFrame>
  )
}

/** The catalog's loading and failed states, shared by the steps that show models. */
export function CatalogState({
  catalog,
  failed,
  onReload,
  label,
}: {
  catalog: Catalog | null
  failed: boolean
  onReload(): void
  label: string
}) {
  if (catalog) return null
  if (failed) {
    return (
      <div className="note error" role="alert">
        <AlertIcon />
        <span>{MODELS_ERRORS.load_failed}</span>
        <button type="button" className="btn ghost" onClick={onReload}>
          Retry
        </button>
      </div>
    )
  }
  return (
    <div className="device-list skeleton" aria-busy="true" aria-label={label}>
      <span className="bar w30" />
      <span className="bar w60" />
    </div>
  )
}
