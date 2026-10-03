import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { effectScope } from 'vue'
import { configureApi, requestOptions, api, ApiError, query, imageBlob } from './api'
import { createSseParser } from './sse'
import { currentSession, animationState } from './runtimeView'
import { applySnapshot, runtime, disconnectRuntime } from '../composables/useRuntime'
import { useRequest } from '../composables/useRequest'
import type { RuntimeMachine, Session, Snapshot } from './types'
const machine: RuntimeMachine = {
  id: '1',
  machine_name: '测试机',
  camera_serial: 'CAM',
  frequency_meter_serial: 'FREQ',
  enabled: true,
  camera_state: 'capturing',
  camera_error: '',
  status: 'online',
  warning: '',
  active_session_id: 'new',
  waiting_cycle_reset: false,
  inflight_count: 2,
}
const session = (id: string): Session => ({
  machine_id: '1',
  session_id: id,
  state: 'RUNNING',
  cycle_closed: false,
  stages: { image_capture: 'running', frequency_collection: 'running' },
  recognized_lines: [],
  final_frequency_hz: null,
  start_time: null,
  finish_time: null,
  errors: [],
})
const snapshot = (sequence = 1): Snapshot => ({
  status: 'running',
  running: true,
  failure: '',
  started_at: null,
  sequence,
  machines: [machine],
  sessions: [session('old'), session('new')],
})
beforeEach(() => {
  configureApi('http://127.0.0.1:1234/api/v1', 'private-token')
  runtime.value = null
})
afterEach(() => {
  vi.unstubAllGlobals()
  disconnectRuntime()
})
describe('API 边界', () => {
  it('保留鉴权头并消除重复 API 前缀，不将令牌加入地址', () => {
    const options = requestOptions('/state')
    expect(options.url).toBe('http://127.0.0.1:1234/api/v1/state')
    expect(options.headers.Authorization).toBe('Bearer private-token')
    expect(options.url).not.toContain('private-token')
    expect(requestOptions('/api/v1/records/a/images/b').url).toBe(
      'http://127.0.0.1:1234/api/v1/records/a/images/b',
    )
  })
  it('保留服务端错误字段，不重写业务错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ success: false, data: { field: 'camera_gain' }, message: '增益无效' }),
            { status: 400 },
          ),
        ),
    )
    await expect(api('/configuration')).rejects.toMatchObject({
      name: 'ApiError',
      message: '增益无效',
      field: 'camera_gain',
      status: 400,
    })
  })
  it('非 JSON 响应提供可理解的错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('unavailable', { status: 503 })))
    await expect(api('/state')).rejects.toBeInstanceOf(ApiError)
  })
  it('图片鉴权保留在请求头且可取消', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(new Blob(['image'])))
    vi.stubGlobal('fetch', fetch)
    const controller = new AbortController()
    await imageBlob('/api/v1/records/a/images/b', controller.signal)
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:1234/api/v1/records/a/images/b', {
      headers: { Authorization: 'Bearer private-token' },
      signal: controller.signal,
    })
  })
  it('筛选传递原始用户文字，编码后由后端规范化', () => {
    expect(query({ text_query: ' ab +c ', page: 2, empty: '', absent: undefined })).toBe(
      '?text_query=+ab+%2Bc+&page=2',
    )
  })
})
describe('SSE 分片与快照', () => {
  it('支持跨网络分片和 CRLF，忽略心跳，保持多行数据', () => {
    const received: unknown[] = []
    const parser = createSseParser((value) => received.push(value))
    parser.push(': heartbeat\r\n\r\nevent: snap')
    parser.push('shot\r\nid: 7\r\ndata: first\r\ndata: second\r\n')
    parser.push('\r\n')
    expect(received).toEqual([{ event: 'snapshot', id: '7', data: 'first\nsecond' }])
  })
  it('拒绝倒退快照，重连明确允许序号重置', () => {
    expect(applySnapshot(snapshot(5))).toBe(true)
    expect(applySnapshot(snapshot(2))).toBe(false)
    expect(runtime.value?.sequence).toBe(5)
    expect(applySnapshot(snapshot(1), true)).toBe(true)
  })
})
describe('周期展示隔离', () => {
  it('新周期开始后不使用旧周期的文字或频率', () => {
    const value = snapshot()
    value.sessions[0].recognized_lines = ['OLD']
    value.sessions[0].final_frequency_hz = 50
    expect(currentSession(value, machine)?.recognized_lines).toEqual([])
    expect(currentSession(value, machine)?.final_frequency_hz).toBeNull()
  })
  it('关闭后的最新结果可见，但不驱动现场动画', () => {
    const value = snapshot()
    const inactive = { ...machine, active_session_id: null }
    expect(currentSession(value, inactive)?.session_id).toBe('new')
    expect(animationState(value, inactive)).toEqual({
      extended: false,
      capturing: false,
      frequencyListening: false,
    })
  })
  it.each(['stopped', 'failed', 'stopping'] as const)(
    '%s 时未关闭的旧周期也必须停止收回',
    (status) => {
      const value = snapshot()
      value.status = status
      expect(animationState(value, machine)).toEqual({
        extended: false,
        capturing: false,
        frequencyListening: false,
      })
    },
  )
  it('找不到新周期时不退回旧结果', () => {
    expect(currentSession(snapshot(), { ...machine, active_session_id: 'missing' })).toBeUndefined()
  })
})
describe('请求竞态', () => {
  it('较慢的旧读取不能覆盖新结果', async () => {
    const scope = effectScope()
    const request = scope.run(() => useRequest<string>())!
    let finishOld!: (value: string) => void
    const previous = request.run(
      () =>
        new Promise((resolve) => {
          finishOld = resolve
        }),
    )
    await request.run(async () => 'new')
    finishOld('old')
    await previous
    expect(request.data.value).toBe('new')
    expect(request.loading.value).toBe(false)
    scope.stop()
  })
  it('取消后迟到结果不会写回', async () => {
    const scope = effectScope()
    const request = scope.run(() => useRequest<string>())!
    let finish!: (value: string) => void
    const pending = request.run(
      () =>
        new Promise((resolve) => {
          finish = resolve
        }),
    )
    request.cancel()
    finish('obsolete')
    await pending
    expect(request.data.value).toBeNull()
    scope.stop()
  })
})
