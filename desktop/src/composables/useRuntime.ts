import { ref, shallowRef } from 'vue'
import { api, errorText, requestOptions } from '../lib/api'
import { createSseParser } from '../lib/sse'
import { writeRuntimeDiagnostic } from '../lib/runtimeDiagnostics'
import type { Envelope, Snapshot } from '../lib/types'
export const runtime = shallowRef<Snapshot | null>(null)
export const connection = ref<'connecting' | 'connected' | 'reconnecting' | 'closed'>('closed')
export const runtimeError = ref('')
export const lastUpdate = ref<Date | null>(null)
export const runtimeAction = ref(false)
let streamController: AbortController | null = null
let generation = 0
/**
 * 更新完整运行快照，并记录首次收到的测量启动状态。
 * Args:
 *   snapshot: 后端返回的完整机器和周期快照。
 *   reset: 是否在重新连接时允许快照编号重置。
 * Returns:
 *   true // 当前快照已更新
 *   false // 当前快照编号倒退，已忽略
 */
export function applySnapshot(snapshot: Snapshot, reset = false) {
  if (!reset && runtime.value && snapshot.sequence < runtime.value.sequence) return false

  // 保存上一份状态并更新当前完整快照和接收时间。
  const previousSnapshot = runtime.value
  runtime.value = snapshot
  lastUpdate.value = new Date()

  // 只在首次收到新周期或重新同步时记录测量状态。
  for (const machine of snapshot.machines) {
    const sessionId = machine.active_session_id
    if (!sessionId) continue
    const previousMachine = previousSnapshot?.machines.find((item) => item.id === machine.id)
    if (!reset && previousMachine?.active_session_id === sessionId) continue

    // 记录周期身份、快照编号和距后端创建测量的耗时。
    const session = snapshot.sessions.find((item) => item.session_id === sessionId)
    writeRuntimeDiagnostic('MEASUREMENT_STATE_RECEIVED', {
      machine_id: machine.id,
      session_id: sessionId,
      sequence: snapshot.sequence,
      status: snapshot.status,
      resynchronized: reset,
      cycle_closed: session?.cycle_closed ?? null,
      measurement_started_at: session?.start_time ?? null,
      since_measurement_start_ms: session?.start_time
        ? Date.now() - Date.parse(session.start_time)
        : null,
    })
  }
  return true
}
/** 重连时重新读取快照，再消费仅包含业务状态的 SSE。Args: 无。Returns: 循环退出时完成。 */
export async function connectRuntime() {
  disconnectRuntime()
  const current = ++generation
  streamController = new AbortController()
  const signal = streamController.signal
  let retry = 0
  while (!signal.aborted && current === generation) {
    connection.value = retry ? 'reconnecting' : 'connecting'
    try {
      const snapshot = await api<Snapshot>('/state', { signal })
      if (signal.aborted || current !== generation) return
      applySnapshot(snapshot, true)
      const endpoint = requestOptions('/events')
      const response = await fetch(endpoint.url, {
        headers: { ...endpoint.headers, Accept: 'text/event-stream' },
        signal,
      })
      if (!response.ok || !response.body) throw new Error(`实时连接失败（${response.status}）`)
      connection.value = 'connected'
      runtimeError.value = ''
      retry = 0
      const parser = createSseParser((event) => {
        if (event.event !== 'snapshot' || signal.aborted || current !== generation) return
        const envelope = JSON.parse(event.data) as Envelope<Snapshot>
        if (envelope.success) applySnapshot(envelope.data)
      })
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      try {
        while (!signal.aborted) {
          const next = await reader.read()
          if (next.done) break
          parser.push(decoder.decode(next.value, { stream: true }))
        }
      } finally {
        await reader.cancel().catch(() => {})
        reader.releaseLock()
      }
      if (!signal.aborted) throw new Error('实时连接已断开，正在重新同步。')
    } catch (error) {
      if (signal.aborted || current !== generation) return
      runtimeError.value = errorText(error)
      connection.value = 'reconnecting'
      retry++
      await new Promise<void>((resolve) => {
        const finish = () => {
          clearTimeout(timer)
          signal.removeEventListener('abort', finish)
          resolve()
        }
        const timer = setTimeout(finish, Math.min(1000 * 2 ** Math.min(retry - 1, 4), 15000))
        signal.addEventListener('abort', finish, { once: true })
      })
    }
  }
}
/**
 * 中止当前实时连接与待重连任务。
 * Args: 无。
 * Returns: 无；connection 更新为 closed。
 */
export function disconnectRuntime() {
  generation++
  streamController?.abort()
  streamController = null
  connection.value = 'closed'
}
/** 只发送监测启停请求，实际状态以快照为准。Args: start 或 stop。Returns: 无。 */
export async function controlMonitoring(action: 'start' | 'stop') {
  if (runtimeAction.value) return
  runtimeAction.value = true
  try {
    applySnapshot(await api<Snapshot>(`/monitoring/${action}`, { method: 'POST' }))
    runtimeError.value = ''
  } catch (error) {
    runtimeError.value = errorText(error)
  } finally {
    runtimeAction.value = false
  }
}
