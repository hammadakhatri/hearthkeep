/** Files attached to a chat for analysis: upload, list, download, and the message line that carries one. */

import { api } from './client'
import { ATTACHED_PHOTO, PHOTO_ACCEPT } from '../components/Photo'

export type ChatFileKind = 'csv' | 'xlsx' | 'xls' | 'pdf' | 'txt' | 'json'

export type ChatFileInfo = {
  id: string
  chat_id: string
  name: string
  kind: ChatFileKind
  size: number
  status: 'ready' | 'unchecked'
  created_at: string
}

/** At most this many files ride along with one message. */
export const MAX_FILES = 5

/** What the picker offers: the six file kinds (by extension and type) plus the photo types. */
export const FILE_ACCEPT = [
  '.csv,text/csv',
  '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.xls,application/vnd.ms-excel',
  '.pdf,application/pdf',
  '.txt,text/plain',
  '.json,application/json',
  PHOTO_ACCEPT,
].join(',')

/** A message line that carries a file. Only lines matching exactly are files. */
export const ATTACHED_FILE = /^Attached file: ([A-Za-z0-9_-]{1,32})$/

export function attachedFileLine(id: string): string {
  return `Attached file: ${id}`
}

/** A message's text split into photos, files and the words the owner wrote. */
export function splitAttachments(text: string): { photos: string[]; files: string[]; text: string } {
  const photos: string[] = []
  const files: string[] = []
  const kept = text.split('\n').filter((line) => {
    const photo = ATTACHED_PHOTO.exec(line)
    if (photo) {
      photos.push(photo[1])
      return false
    }
    const file = ATTACHED_FILE.exec(line)
    if (file) {
      files.push(file[1])
      return false
    }
    return true
  })
  return { photos, files, text: kept.join('\n').trim() }
}

const PHOTO_TYPES = new Set(PHOTO_ACCEPT.split(','))
const PHOTO_EXTENSION = /\.(png|jpe?g|webp)$/i

/** Photos keep the photo path (they can't be analysed as data); everything else is a chat file. */
export function isPhotoFile(file: File): boolean {
  return PHOTO_TYPES.has(file.type) || (file.type === '' && PHOTO_EXTENSION.test(file.name))
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${Math.max(0, Math.round(bytes))} B`
  if (bytes < 1000 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function fileDownloadUrl(id: string): string {
  return `/api/files/${encodeURIComponent(id)}/download`
}

export function uploadChatFile(chatId: string, file: File): Promise<ChatFileInfo> {
  const form = new FormData()
  form.append('file', file)
  return api<ChatFileInfo>(`/api/chats/${encodeURIComponent(chatId)}/files`, { method: 'POST', body: form })
}

/** The server answers `{files: [...]}`. */
export async function listChatFiles(chatId: string): Promise<ChatFileInfo[]> {
  const body = await api<{ files: ChatFileInfo[] }>(`/api/chats/${encodeURIComponent(chatId)}/files`)
  return body.files
}
