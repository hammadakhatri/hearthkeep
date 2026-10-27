import { useCallback, useRef, useState } from 'react'
import { isApiError } from '../api/client'
import {
  MAX_FILES,
  fileDownloadUrl,
  formatSize,
  uploadChatFile,
  type ChatFileInfo,
  type ChatFileKind,
} from '../api/files'
import { AlertIcon, CloseIcon, DownloadIcon, FileIcon } from '../shell/icons'

export const KIND_LABEL: Record<ChatFileKind, string> = {
  csv: 'CSV',
  xlsx: 'Excel',
  xls: 'Excel',
  pdf: 'PDF',
  txt: 'Text',
  json: 'JSON',
}

export const UNCHECKED_TIP = "Hearthkeep couldn't open it in the sandbox; it can still try when you ask."
export const TOO_MANY_FILES = `Attach up to ${MAX_FILES} files at a time.`

/** One chip: a file being uploaded, one that failed, or one the server holds. */
export type FileChipItem = {
  /** Identifies the chip in lists (the file id once known). */
  key: string
  name: string
  /** `deleted`: a stored file that is gone; shown by name only, nothing to do with it. */
  state: 'uploading' | 'failed' | 'ready' | 'unchecked' | 'deleted'
  /** The server's id: present once uploaded; it is what the download link uses. */
  id?: string
  kind?: ChatFileKind
  size?: number
  /** Why the upload failed, as the server worded it. */
  message?: string
}

export function chipFromInfo(info: ChatFileInfo): FileChipItem {
  return {
    key: info.id,
    id: info.id,
    name: info.name,
    kind: info.kind,
    size: info.size,
    state: info.status === 'unchecked' ? 'unchecked' : 'ready',
  }
}

/** The files as a list of chips: name, type, size, download; or a spinner, or why it failed. */
export function FileChips({
  files,
  onRemove,
  onDelete,
  className,
  label = 'Attached files',
}: {
  files: FileChipItem[]
  /** Takes a not-yet-sent file out of the message (composer). */
  onRemove?: (key: string) => void
  /** Deletes a stored file (file lists). */
  onDelete?: (key: string) => void
  className?: string
  /** Names the list; give each list on a page its own. */
  label?: string
}) {
  if (!files.length) return null
  return (
    <ul className={className ? `file-chips ${className}` : 'file-chips'} aria-label={label}>
      {files.map((file) => (
        <li key={file.key} className="file-chip" data-state={file.state}>
          {file.state === 'uploading' ? (
            <span className="file-chip-wait" aria-hidden="true" />
          ) : file.state === 'failed' ? (
            <AlertIcon className="file-chip-icon" />
          ) : (
            <FileIcon className="file-chip-icon" />
          )}
          <span className="file-chip-main">
            <span className="file-chip-name">
              <bdi>{file.name}</bdi>
            </span>
            <span className="file-chip-meta">
              {file.state === 'uploading' && <span>Uploading…</span>}
              {file.state === 'failed' && <span className="file-chip-error">{file.message}</span>}
              {file.state === 'deleted' && <span>Removed from this chat</span>}
              {(file.state === 'ready' || file.state === 'unchecked') && (
                <>
                  {file.kind && <span>{KIND_LABEL[file.kind]}</span>}
                  {file.size !== undefined && <span>{formatSize(file.size)}</span>}
                  {file.state === 'unchecked' && (
                    <span className="file-chip-unchecked" title={UNCHECKED_TIP}>
                      Not checked yet
                    </span>
                  )}
                </>
              )}
            </span>
          </span>
          {file.id && (file.state === 'ready' || file.state === 'unchecked') && (
            <a
              className="iconbtn file-chip-action"
              href={fileDownloadUrl(file.id)}
              download
              aria-label={`Download ${file.name}`}
              title="Download"
            >
              <DownloadIcon />
            </a>
          )}
          {onDelete && file.state !== 'uploading' && file.state !== 'deleted' && (
            <button
              type="button"
              className="iconbtn file-chip-action"
              aria-label={`Delete ${file.name}`}
              onClick={() => onDelete(file.key)}
            >
              <CloseIcon />
            </button>
          )}
          {onRemove && (
            <button
              type="button"
              className="iconbtn file-chip-action"
              aria-label={`Remove ${file.name}`}
              onClick={() => onRemove(file.key)}
            >
              <CloseIcon />
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}

function uploadError(error: unknown, name: string): string {
  if (isApiError(error, 'network')) return `Can't reach Hearthkeep. ${name} wasn't uploaded.`
  if (isApiError(error)) {
    const message = (error.body as { message?: unknown } | null)?.message
    if (typeof message === 'string' && message) return message
  }
  return `${name} couldn't be uploaded. Try again.`
}

/**
 * The files for the next message, each uploaded as soon as it is picked. `announcement` is what
 * the live region says ("Uploading x", "x attached", the refusal, "x removed").
 */
export function useChatFiles() {
  const [files, setFiles] = useState<FileChipItem[]>([])
  const [announcement, setAnnouncement] = useState('')
  const [limitNotice, setLimitNotice] = useState<string | null>(null)
  const generation = useRef(0)
  const live = useRef<FileChipItem[]>([])
  const serial = useRef(0)

  const update = useCallback((next: (all: FileChipItem[]) => FileChipItem[]) => {
    live.current = next(live.current)
    setFiles(live.current)
  }, [])

  /**
   * Upload one file to the chat `chatId()` resolves to (it may have to create the chat first).
   * Resolves with the stored file, or null if it failed, was refused or was dropped meanwhile.
   */
  const add = useCallback(
    async (file: File, chatId: () => Promise<string>): Promise<ChatFileInfo | null> => {
      const held = live.current.filter((f) => f.state !== 'failed').length
      if (held >= MAX_FILES) {
        setLimitNotice(TOO_MANY_FILES)
        setAnnouncement(TOO_MANY_FILES)
        return null
      }
      setLimitNotice(null)
      const token = generation.current
      const key = `pending-${++serial.current}`
      update((all) => [...all, { key, name: file.name, state: 'uploading' }])
      setAnnouncement(`Uploading ${file.name}`)
      try {
        const info = await uploadChatFile(await chatId(), file)
        if (token !== generation.current) return null
        update((all) => all.map((f) => (f.key === key ? chipFromInfo(info) : f)))
        setAnnouncement(`${info.name} attached`)
        return info
      } catch (error) {
        if (token === generation.current) {
          const message = uploadError(error, file.name)
          update((all) => all.map((f) => (f.key === key ? { ...f, state: 'failed', message } : f)))
          setAnnouncement(`${file.name} couldn't be uploaded. ${message}`)
        }
        return null
      }
    },
    [update],
  )

  /** The owner took a file away. */
  const remove = useCallback(
    (key: string) => {
      const gone = live.current.find((f) => f.key === key)
      update((all) => all.filter((f) => f.key !== key))
      setLimitNotice(null)
      setAnnouncement(gone ? `${gone.name} removed` : '')
    },
    [update],
  )

  /** Drop the files that were sent; anything else (failed, picked or uploading since) stays. */
  const sent = useCallback((ids: string[]) => {
    update((all) => all.filter((f) => !(f.id && ids.includes(f.id))))
    setLimitNotice(null)
    setAnnouncement('')
  }, [update])

  /** Drop everything quietly: the owner moved to another chat. */
  const clear = useCallback(() => {
    generation.current += 1
    update(() => [])
    setLimitNotice(null)
    setAnnouncement('')
  }, [update])

  return {
    files,
    announcement,
    limitNotice,
    uploading: files.some((f) => f.state === 'uploading'),
    /** Ids of the stored files, in the order they were picked. */
    readyIds: files
      .filter((f) => f.id && (f.state === 'ready' || f.state === 'unchecked'))
      .map((f) => f.id as string),
    add,
    remove,
    sent,
    clear,
  }
}

/** The composer's chips, plus the "up to 5" note. */
export function AttachedFiles({ attach }: { attach: ReturnType<typeof useChatFiles> }) {
  return (
    <>
      {attach.limitNotice && (
        // Announced once through the live region (see announcement); not an alert as well.
        <div className="note error composer-notice">
          <AlertIcon />
          <span>{attach.limitNotice}</span>
        </div>
      )}
      <FileChips files={attach.files} onRemove={attach.remove} label="Files attached to this message" />
    </>
  )
}
