import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureApi } from './api'
import { connectRuntime, connection, disconnectRuntime, runtime } from '../composables/useRuntime'
import type { Snapshot } from './types'
const makeSnapshot = (sequence: number): Snapshot => ({
  sequence,
  status: 'stopped',
  running: false,
  started_at: null,
  failure: '',
  machines: [],
  sessions: [],
})
const envelope = (sequence: number) => ({
  success: true,
  data: makeSnapshot(sequence),
  message: '',
})

afterEach(() => {
  disconnectRuntime()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('实时断线恢复', () => {
  it('每次重连都先获取快照，并允许新进程序号从头开始', async () => {
    vi.useFakeTimers()
    configureApi('', 'secret')
    runtime.value = null
    const encoder = new TextEncoder()
    let streams = 0
    let snapshots = 0
    const fetch = vi.fn(async (url: string, options: RequestInit) => {
      if (url.endsWith('/state'))
        return new Response(JSON.stringify(envelope(++snapshots === 1 ? 5 : 1)))
      streams++
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                `event: snapshot\nid: ${streams === 1 ? 6 : 2}\ndata: ${JSON.stringify(envelope(streams === 1 ? 6 : 2))}\n\n`,
              ),
            )
            if (streams === 1) controller.close()
            else
              options.signal?.addEventListener(
                'abort',
                () => controller.error(new DOMException('Aborted', 'AbortError')),
                { once: true },
              )
          },
        }),
        { headers: { 'Content-Type': 'text/event-stream' } },
      )
    })
    vi.stubGlobal('fetch', fetch)
    const running = connectRuntime()
    await vi.advanceTimersByTimeAsync(0)
    expect((runtime.value as Snapshot | null)?.sequence).toBe(6)
    expect(connection.value).toBe('reconnecting')
    await vi.advanceTimersByTimeAsync(1000)
    expect(snapshots).toBe(2)
    expect((runtime.value as Snapshot | null)?.sequence).toBe(2)
    expect(connection.value).toBe('connected')
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/state',
      '/api/v1/events',
      '/api/v1/state',
      '/api/v1/events',
    ])
    for (const [, options] of fetch.mock.calls)
      expect((options.headers as Record<string, string>).Authorization).toBe('Bearer secret')
    disconnectRuntime()
    await running
    expect(connection.value).toBe('closed')
  })
})
