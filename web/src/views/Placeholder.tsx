import { VIEWS, type ViewId } from '../shell/views'

/** Stands in for a view that is not built yet. */
export function Placeholder({ view }: { view: ViewId }) {
  const { icon: Icon, blurb } = VIEWS[view]
  return (
    <section className="placeholder" aria-labelledby="placeholder-blurb">
      <div className="placeholder-icon" aria-hidden="true">
        <Icon width={30} height={30} />
      </div>
      <p className="placeholder-kicker">Arrives in a later update</p>
      <h2 id="placeholder-blurb" className="placeholder-blurb">
        {blurb}
      </h2>
    </section>
  )
}
