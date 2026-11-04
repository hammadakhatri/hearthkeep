import type { Addon, SandboxBuildApi } from '../../api/addons'
import type { Catalog, CatalogItem } from '../../api/models'
import { ADDONS_FAILED, dockerHint } from '../../components/AddonsPanel'
import { ImageModelPicker } from '../../components/ModelsPanel'
import { SandboxBuild } from '../../components/SandboxBuild'
import { StartupSwitch, type StartupState } from '../../components/StartupSwitch'
import { AlertIcon } from '../../shell/icons'
import { SETUP_COPY } from './copy'
import { StepFrame } from './StepFrame'

type Props = {
  addons: Addon[] | null
  addonsFailed: boolean
  /** Read the list again; `fresh`: the server looks again first (Check again). */
  onReloadAddons(fresh?: boolean): void
  sandboxApi: SandboxBuildApi
  catalog: Catalog | null
  chosen: readonly string[]
  onChoose(item: CatalogItem): void
  /** Start at login as setup will leave it; `null` while it is read. */
  startup: StartupState | null
  startupBusy: boolean
  startupError: string | null
  onStartup(enabled: boolean): void
  focus: boolean
  onBack(): void
  onNext(): void
}

const CARDS = ['analysis', 'images', 'github', 'devices', 'startup'] as const

/** Step 4: one card each for Analysis, Images, GitHub, Phone and devices, and Start at login. */
export function AddOns(props: Props) {
  const { addons, catalog, focus } = props

  function body(addon: Addon) {
    switch (addon.id) {
      case 'analysis':
        return addon.ready ? (
          <p className="addon-message ready">{addon.message}</p>
        ) : (
          <>
            <p className="addon-message">{addon.message}</p>
            <SandboxBuild
              api={props.sandboxApi}
              docker={dockerHint(addon) ?? 'ready'}
              onCheckAgain={() => props.onReloadAddons(true)}
              onFinished={() => props.onReloadAddons()}
            />
          </>
        )
      case 'images':
        return catalog ? (
          <ImageModelPicker catalog={catalog} action={{ kind: 'choose', chosen: props.chosen, onChoose: props.onChoose }} />
        ) : null
      case 'github':
        return <p className="addon-message later">{SETUP_COPY.github}</p>
      case 'devices':
        return <p className="addon-message later">{SETUP_COPY.phone}</p>
      case 'startup':
        return (
          <StartupSwitch
            state={props.startup}
            onChange={props.onStartup}
            busy={props.startupBusy}
            error={props.startupError}
          />
        )
      default:
        return null
    }
  }

  // Start at login is built in only on Windows: elsewhere its card isn't shown.
  const cards = (addons ?? []).filter(
    (a) => (CARDS as readonly string[]).includes(a.id) && !(a.id === 'startup' && a.state === 'unsupported'),
  )
  return (
    <StepFrame title={SETUP_COPY.addonsTitle} focus={focus} onBack={props.onBack} nextLabel="Next" onNext={props.onNext}>
      <p className="setup-lede">{SETUP_COPY.addonsLede}</p>
      {props.addonsFailed && !addons && (
        <div className="note error" role="alert">
          <AlertIcon />
          <span>{ADDONS_FAILED}</span>
          <button type="button" className="btn ghost" onClick={() => props.onReloadAddons()}>
            Retry
          </button>
        </div>
      )}
      {!addons && !props.addonsFailed && (
        <div className="device-list skeleton" role="status" aria-busy="true" aria-label="Loading add-ons">
          <span className="bar w30" />
          <span className="bar w60" />
        </div>
      )}
      <div className="setup-addons">
        {cards.map((addon) => (
          <article key={addon.id} className="tile addon" aria-labelledby={`setup-addon-${addon.id}`}>
            <div className="addon-top">
              <h3 id={`setup-addon-${addon.id}`}>{addon.name}</h3>
            </div>
            <p className="addon-about">{addon.about}</p>
            <p className="addon-needs">Needs: {addon.requires}</p>
            {body(addon)}
          </article>
        ))}
      </div>
    </StepFrame>
  )
}
