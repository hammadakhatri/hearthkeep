import { act } from '@testing-library/react'
import { vi } from 'vitest'

type Listener = (event: MessageEvent) => void

/** A stand-in for the browser's EventSource that tests drive by hand. */
export class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  readyState = 0
  onerror: ((event: Event) => void) | null = null
  onopen: ((event: Event) => void) | null = null
  private listeners = new Map<string, Set<Listener>>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: Listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type)!.add(listener)
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners.get(type)?.delete(listener)
  }

  close() {
    this.readyState = 2
  }

  get closed() {
    return this.readyState === 2
  }

  /** Deliver one server-sent event (`id: seq`, `event: type`, `data: json`). */
  emit(type: string, seq: number, data: Record<string, unknown> = {}) {
    if (this.closed) return
    const event = new MessageEvent(type, { data: JSON.stringify(data), lastEventId: String(seq) })
    this.listeners.get(type)?.forEach((listener) => listener(event))
  }

  /** The connection dropped. */
  fail() {
    this.onerror?.(new Event('error'))
  }
}

export function installEventSource() {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  return FakeEventSource
}

export function lastSource(): FakeEventSource {
  const source = FakeEventSource.instances[FakeEventSource.instances.length - 1]
  if (!source) throw new Error('no EventSource was opened')
  return source
}

/** Emit inside act() so React applies the update before the test looks. */
export function emit(type: string, seq: number, data: Record<string, unknown> = {}) {
  act(() => lastSource().emit(type, seq, data))
}
