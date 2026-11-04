import { formatBytes, type Job, type JobItem } from '../api/models'
import { CheckIcon } from '../shell/icons'

/** A plain sentence for each reason an item can fail (the installer's codes). */
export const ITEM_ERRORS: Record<string, string> = {
  network: 'The download stopped. Retry to continue from where it stopped.',
  not_found: 'This file is no longer at its download address. Hearthkeep needs an update to fetch it.',
  too_big: 'This file was larger than expected, so it was removed. Hearthkeep needs an update to fetch it.',
  checksum: "The file didn't match its checksum, so it was removed. Retry to download it again.",
  no_space: "There isn't enough free space on this PC. Free some space, then retry.",
  unpack: "The download couldn't be unpacked. Retry, or check that antivirus isn't blocking it.",
  config_not_updated:
    "The files are ready, but Hearthkeep couldn't update its model settings file. Retry, or ask for help with the file.",
  cancelled: 'Stopped',
}

const UNKNOWN_ERROR = "The download didn't finish. Retry to try again."

function fraction(item: JobItem): number {
  if (item.state === 'done') return 1
  if (!(item.total_bytes > 0)) return 0
  return Math.min(1, Math.max(0, item.done_bytes / item.total_bytes))
}

function Verified() {
  return (
    <span className="download-verified">
      <CheckIcon width={14} height={14} />
      Verified
    </span>
  )
}

/** What the item is doing now, in a few words. */
function ItemState({ item }: { item: JobItem }) {
  switch (item.state) {
    case 'queued':
      return <span>Waiting</span>
    case 'downloading':
      return <span>{item.speed_bps > 0 ? `${formatBytes(item.speed_bps)}/s` : 'Downloading'}</span>
    case 'verifying':
      return <span>Checking the file</span>
    case 'unpacking':
      return (
        <>
          <Verified />
          <span>Unpacking</span>
        </>
      )
    case 'done':
      return <Verified />
    case 'cancelled':
      return <span>Stopped</span>
    default:
      return null
  }
}

type Props = { job: Job; onRetry(): void; onCancel(): void }

/** One row per item of an install job: size, progress, speed, Verified once checked, and a plain
 *  sentence for an item that failed; one Retry for the job. Presentational: the caller polls and
 *  acts. */
export function DownloadList({ job, onRetry, onCancel }: Props) {
  const done = job.items.filter((i) => i.state === 'done').length
  const failed = job.items.filter((i) => i.state === 'failed' && i.error !== 'cancelled').length
  const running = job.state === 'running'
  // The server retries every failed and stopped item of the job at once: one button for all.
  const canRetry = !running && job.items.some((i) => i.state === 'failed' || i.state === 'cancelled')
  return (
    <div className="downloads">
      <div className="downloads-head">
        <p className="downloads-summary" aria-live="polite">
          {`${done} of ${job.items.length} done${failed > 0 ? `, ${failed} failed` : ''}`}
        </p>
        {running && (
          <button type="button" className="btn ghost" onClick={onCancel}>
            Stop downloading
          </button>
        )}
        {canRetry && (
          <button type="button" className="btn" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
      <ul className="device-list">
        {job.items.map((item) => {
          const part = fraction(item)
          const percent = Math.round(part * 100)
          const stopped = item.state === 'cancelled' || item.error === 'cancelled'
          const itemFailed = item.state === 'failed' && !stopped
          return (
            <li key={item.id} className="device download">
              <div className="download-top">
                <b className="download-name">{item.name}</b>
                <span className="download-size">{formatBytes(item.total_bytes)}</span>
              </div>
              <div
                className="download-bar"
                role="progressbar"
                aria-label={item.name}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={percent}
                aria-valuetext={`${percent}%, ${formatBytes(item.done_bytes)} of ${formatBytes(item.total_bytes)}`}
              >
                <span className="download-fill" style={{ transform: `scaleX(${part})` }} />
              </div>
              <div className="download-state">
                {stopped ? <span>{ITEM_ERRORS.cancelled}</span> : <ItemState item={item} />}
              </div>
              {itemFailed && (
                <p className="download-error">{(item.error && ITEM_ERRORS[item.error]) || UNKNOWN_ERROR}</p>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
