import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref, type Component } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { configureApi } from '../lib/api'
import { connection, runtime, runtimeAction, runtimeError } from '../composables/useRuntime'
import type { AbnormalEvent, Configuration, Machine, Measurement, Snapshot } from '../lib/types'
import HistoryView from './HistoryView.vue'
import MachinesView from './MachinesView.vue'
import ConfigurationView from './ConfigurationView.vue'
import AbnormalView from './AbnormalView.vue'
import ImagesView from './ImagesView.vue'
import RealtimeView from './RealtimeView.vue'

// 只替换浮层容器，保留页面表单、标签页和证据组件的真实逻辑。
const DialogHarness = defineComponent({
  props: ['open', 'title', 'description'],
  emits: ['update:open'],
  template: `<section v-if="open" role="dialog" :aria-label="title">
    <h2>{{ title }}</h2><p>{{ description }}</p>
    <button aria-label="关闭对话框" @click="$emit('update:open', false)">关闭</button>
    <slot />
  </section>`,
})
const mountedPages: VueWrapper[] = []
const requests: { url: URL; options: RequestInit }[] = []
let respond: (url: URL, options: RequestInit) => Response | Promise<Response>
const observeCallbacks: (() => void)[] = []

/**
 * 挂载真实页面并记录清理对象。
 * Args:
 *   component: 要测试的页面组件。
 * Returns:
 *   VueWrapper // 可查询页面节点的测试包装器
 */
function mountPage(component: Component) {
  const wrapper = mount(component, {
    attachTo: document.body,
    global: {
      stubs: { AppDialog: DialogHarness, RouterLink: true },
    },
  })
  mountedPages.push(wrapper)
  return wrapper
}

/**
 * 创建后端统一成功响应。
 * Args:
 *   data: 本次接口返回的数据。
 * Returns:
 *   Response // 包含 success、data 和 message 的 JSON 响应
 */
function createResponse(data: unknown) {
  return new Response(JSON.stringify({ success: true, data, message: '' }))
}

/**
 * 创建后端字段错误响应。
 * Args:
 *   message: 显示给用户的错误。
 *   status: HTTP 状态码。
 *   field: 出错字段。
 * Returns:
 *   Response // 包含错误信息和字段的 JSON 响应
 */
function createFailure(message: string, status: number, field?: string) {
  return new Response(JSON.stringify({ success: false, message, data: { field } }), { status })
}

/**
 * 生成包含正式文字和复核状态的测量记录。
 * Args:
 *   sessionId: 测量周期编号。
 *   overrides: 覆盖默认记录的字段。
 * Returns:
 *   Measurement // 含周期、机器、文字、频率和复核字段的完整记录
 */
function createRecord(sessionId: string, overrides: Partial<Measurement> = {}): Measurement {
  return {
    session_id: sessionId,
    machine_id: '1',
    machine_name: '一号机',
    start_time: '2026-10-03T10:00:00Z',
    finish_time: '2026-10-03T10:00:05Z',
    recognized_lines: [`ORIGINAL-${sessionId}`],
    final_frequency_hz: 52.5,
    evidence_directory: '/evidence',
    needs_review: true,
    review_reason: '请核实文字',
    reviewed_at: null,
    reviewed_lines: null,
    ...overrides,
  }
}

/**
 * 生成服务端分页测量响应。
 * Args:
 *   records: 当前页记录。
 *   page: 当前页码。
 *   totalPages: 服务端总页数。
 * Returns:
 *   Response // 含 records、page、page_size、total 和 total_pages 的响应
 */
function createRecordPage(records: Measurement[], page = 1, totalPages = 1) {
  return createResponse({
    records,
    page,
    page_size: 20,
    total: totalPages === 1 ? records.length : totalPages * 20,
    total_pages: totalPages,
  })
}

/**
 * 按显示文字获取页面按钮。
 * Args:
 *   wrapper: 当前页面包装器。
 *   label: 按钮完整文字。
 * Returns:
 *   DOMWrapper // 对应的按钮节点
 */
function getButton(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text() === label)
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}

/**
 * 生成可手动完成的接口读取。
 * Args:
 *   无外部参数。
 * Returns:
 *   object // 可等待并手动结束的响应
 *     promise // 待完成的读取
 *     resolve // 提交本次响应
 */
function createDeferredResponse() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>((complete) => {
    resolve = complete
  })
  return { promise, resolve }
}

const machine: Machine = {
  id: 1,
  machine_name: '一号机',
  camera_serial: 'CAM-1',
  frequency_meter_serial: 'FREQ-1',
  enabled: true,
  remark: '生产线 A',
}
const settings: Configuration = {
  camera_exposure_time_us: 1200,
  camera_gain: null,
  camera_line_selector: null,
  camera_line_mode: null,
  camera_line_source: null,
  camera_strobe_enabled: null,
  modbus_serial_port: 'COM3',
  modbus_baudrate: 9600,
  modbus_unit_id: 1,
  minimum_frequency_hz: 10,
  maximum_frequency_hz: 100,
  ocr_lock_wait_timeout_ms: 1000,
  ocr_result_timeout_ms: 5000,
  max_cycle_open_ms: 15000,
  io_machine_channels: { '1': 0 },
  database_path: '/private/measurements.db',
}

beforeEach(() => {
  // 每个测试使用独立的连接状态和可审计请求列表。
  configureApi('http://127.0.0.1:8765', 'workflow-token')
  runtime.value = null
  runtimeAction.value = false
  runtimeError.value = ''
  connection.value = 'connected'
  requests.length = 0
  observeCallbacks.length = 0
  respond = (url) => {
    if (url.pathname === '/api/v1/records/machines') {
      return createResponse({ machines: [{ machine_id: '1', machine_name: '一号机' }] })
    }
    throw new Error(`未配置接口：${url.pathname}${url.search}`)
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: string, options: RequestInit = {}) => {
      const url = new URL(input)
      requests.push({ url, options })
      return Promise.resolve(respond(url, options))
    }),
  )

  // 手动控制可见区域，避免 jsdom 提前触发所有图片读取。
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(callback: IntersectionObserverCallback) {
        observeCallbacks.push(() =>
          callback(
            [{ isIntersecting: true } as IntersectionObserverEntry],
            this as unknown as IntersectionObserver,
          ),
        )
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    },
  )
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  )
})

afterEach(() => {
  mountedPages.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('历史页面工作流', () => {
  it('查询才应用筛选，翻页沿用已应用值，重置返回第一页', async () => {
    const defaultRespond = respond
    respond = (url, options) =>
      url.pathname === '/api/v1/records'
        ? createRecordPage(
            [createRecord(`page-${url.searchParams.get('page')}`)],
            Number(url.searchParams.get('page')),
            3,
          )
        : defaultRespond(url, options)
    const wrapper = mountPage(HistoryView)
    await flushPromises()

    // 编辑筛选不立即请求服务器。
    const initialCount = requests.length
    await wrapper.get('[aria-label="搜索识别文字"]').setValue(' ab + c ')
    await wrapper.get('#record-review').setValue('pending')
    await wrapper.get('#record-machine').setValue('1')
    expect(requests).toHaveLength(initialCount)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    let latest = requests.filter((request) => request.url.pathname === '/api/v1/records').at(-1)!
    expect(Object.fromEntries(latest.url.searchParams)).toMatchObject({
      text_query: ' ab + c ',
      review_status: 'pending',
      machine_id: '1',
      page: '1',
      page_size: '20',
    })

    // 翻页使用已提交条件，不偷用仍在编辑的文字。
    await wrapper.get('[aria-label="搜索识别文字"]').setValue('尚未提交')
    await wrapper.get('[aria-label="下一页"]').trigger('click')
    await flushPromises()
    latest = requests.filter((request) => request.url.pathname === '/api/v1/records').at(-1)!
    expect(latest.url.searchParams.get('text_query')).toBe(' ab + c ')
    expect(latest.url.searchParams.get('page')).toBe('2')
    expect(wrapper.text()).toContain('第 2 / 3 页')

    await getButton(wrapper, '更多筛选').trigger('click')
    await getButton(wrapper, '重置').trigger('click')
    await flushPromises()
    latest = requests.filter((request) => request.url.pathname === '/api/v1/records').at(-1)!
    expect(latest.url.searchParams.has('text_query')).toBe(false)
    expect(latest.url.searchParams.get('page')).toBe('1')
    expect((wrapper.get('[aria-label="搜索识别文字"]').element as HTMLInputElement).value).toBe('')
  })

  it('按周期保留复核草稿，服务端失败后可原样重试并只刷新保存结果', async () => {
    let record = createRecord('review-1')
    let failSave = true
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage([record])
      if (url.pathname === '/api/v1/records/review-1') return createResponse({ record })
      if (url.pathname === '/api/v1/records/review-1/review') {
        if (failSave) return createFailure('复核保存失败，请重试', 503)
        record = { ...record, reviewed_at: '2026-10-03T11:00:00Z', reviewed_lines: ['CORRECTED'] }
        return createResponse({ record })
      }
      return defaultRespond(url, options)
    }
    const wrapper = mountPage(HistoryView)
    await flushPromises()
    await wrapper.get('[aria-label="查看测量 review-1"]').trigger('click')
    await flushPromises()
    expect(requests.some((request) => request.url.pathname.endsWith('/evidence'))).toBe(false)

    await wrapper.get('[role="dialog"] input[type="checkbox"]').setValue(true)
    await wrapper.get('textarea').setValue(' corrected \n second ')
    await wrapper.get('[aria-label="关闭对话框"]').trigger('click')
    await wrapper.get('[aria-label="查看测量 review-1"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe(
      ' corrected \n second ',
    )
    await getButton(wrapper, '保存修正并完成复核').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('复核保存失败')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe(
      ' corrected \n second ',
    )

    // 服务端接收原始草稿，前端不自行规范化复核结果。
    failSave = false
    await getButton(wrapper, '保存修正并完成复核').trigger('click')
    await flushPromises()
    const reviewRequests = requests.filter((request) => request.url.pathname.endsWith('/review'))
    expect(reviewRequests).toHaveLength(2)
    expect(JSON.parse(reviewRequests[1].options.body as string)).toEqual({
      edited_text: ' corrected \n second ',
    })
    expect(wrapper.text()).toContain('复核结果已保存。')
    expect(wrapper.text()).toContain('CORRECTED')
    expect(wrapper.find('textarea').exists()).toBe(false)
  })

  it('关闭并重开详情不覆盖用户主动清空的复核草稿', async () => {
    const record = createRecord('blank-draft')
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage([record])
      if (url.pathname === '/api/v1/records/blank-draft') return createResponse({ record })
      return defaultRespond(url, options)
    }
    const wrapper = mountPage(HistoryView)
    await flushPromises()
    await wrapper.get('[aria-label="查看测量 blank-draft"]').trigger('click')
    await flushPromises()
    await wrapper.get('[role="dialog"] input[type="checkbox"]').setValue(true)
    await wrapper.get('textarea').setValue('')
    await wrapper.get('[aria-label="关闭对话框"]').trigger('click')
    await wrapper.get('[aria-label="查看测量 blank-draft"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
  })
})

describe('历史页面延迟读取与缓存', () => {
  it('图片标签页首次激活才读取证据，返回识别页取消进行中的读取', async () => {
    const record = createRecord('tab-evidence')
    const evidenceResponse = createDeferredResponse()
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage([record])
      if (url.pathname === '/api/v1/records/tab-evidence') return createResponse({ record })
      if (url.pathname.endsWith('/evidence')) return evidenceResponse.promise
      return defaultRespond(url, options)
    }
    const wrapper = mountPage(HistoryView)
    await flushPromises()
    await wrapper.get('[aria-label="查看测量 tab-evidence"]').trigger('click')
    await flushPromises()
    expect(requests.some((request) => request.url.pathname.endsWith('/evidence'))).toBe(false)

    // 通过真实 Reka 标签页鼠标交互切换证据内容。
    await getButton(wrapper, '图片证据').trigger('mousedown', { button: 0, ctrlKey: false })
    await flushPromises()
    const evidenceRequest = requests.find((request) => request.url.pathname.endsWith('/evidence'))!
    expect(evidenceRequest.url.searchParams.get('page_size')).toBe('24')
    await getButton(wrapper, '识别与复核').trigger('mousedown', { button: 0, ctrlKey: false })
    await flushPromises()
    expect(evidenceRequest.options.signal?.aborted).toBe(true)
    evidenceResponse.resolve(
      createResponse({
        images: [],
        state: 'no_jpg',
        count: 0,
        page: 1,
        page_size: 24,
        total_pages: 1,
      }),
    )
    await flushPromises()
    expect(wrapper.text()).toContain('正式 OCR 结果')
    expect(wrapper.find('[aria-label="证据图片"]').exists()).toBe(false)
  })

  it('缓存离页关闭详情，返回刷新列表后仍保留未提交复核草稿', async () => {
    const record = createRecord('cached-review')
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage([record])
      if (url.pathname === '/api/v1/records/cached-review') return createResponse({ record })
      return defaultRespond(url, options)
    }
    const showHistory = ref(true)
    const navigation = defineComponent({
      components: { HistoryView },
      setup: () => ({ showHistory }),
      template: '<KeepAlive><HistoryView v-if="showHistory" /><div v-else>另一页</div></KeepAlive>',
    })
    const wrapper = mountPage(navigation)
    await flushPromises()
    await wrapper.get('[aria-label="查看测量 cached-review"]').trigger('click')
    await flushPromises()
    await wrapper.get('[role="dialog"] input[type="checkbox"]').setValue(true)
    await wrapper.get('textarea').setValue('待核实的草稿')
    showHistory.value = false
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    showHistory.value = true
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(requests.filter((request) => request.url.pathname === '/api/v1/records')).toHaveLength(2)
    await wrapper.get('[aria-label="查看测量 cached-review"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('待核实的草稿')
    expect(
      requests.filter((request) => request.url.pathname === '/api/v1/records/cached-review'),
    ).toHaveLength(2)
  })
})

describe('机器页面工作流', () => {
  it('新建草稿可关闭后继续，创建成功后清空新建表单', async () => {
    const machines = [machine]
    respond = (url, options) => {
      if (url.pathname === '/api/v1/machines' && options.method === 'POST') {
        machines.push({ id: 2, ...JSON.parse(options.body as string) })
        return createResponse({ machine: machines[1] })
      }
      if (url.pathname === '/api/v1/machines') return createResponse({ machines })
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const wrapper = mountPage(MachinesView)
    await flushPromises()
    await getButton(wrapper, '添加机器').trigger('click')
    const fields = wrapper.findAll('[role="dialog"] input:not([type="checkbox"])')
    await fields[0].setValue('二号机')
    await fields[1].setValue('CAM-2')
    await fields[2].setValue('FREQ-2')
    await wrapper.get('textarea').setValue('新生产线')
    await getButton(wrapper, '稍后继续').trigger('click')
    await getButton(wrapper, '添加机器').trigger('click')
    expect((wrapper.get('[role="dialog"] input').element as HTMLInputElement).value).toBe('二号机')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const create = requests.find((request) => request.options.method === 'POST')!
    expect(JSON.parse(create.options.body as string)).toEqual({
      machine_name: '二号机',
      camera_serial: 'CAM-2',
      frequency_meter_serial: 'FREQ-2',
      enabled: true,
      remark: '新生产线',
    })
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('机器已添加')
    expect(wrapper.findAll('.equipment-card')).toHaveLength(2)
    await getButton(wrapper, '添加机器').trigger('click')
    expect((wrapper.get('[role="dialog"] input').element as HTMLInputElement).value).toBe('')
  })

  it('删除等待期间禁止新编辑，缓存离页后完成删除并在返回时刷新列表', async () => {
    const deleteResponse = createDeferredResponse()
    let removed = false
    respond = (url, options) => {
      if (url.pathname === '/api/v1/machines/1' && options.method === 'DELETE')
        return deleteResponse.promise
      if (url.pathname === '/api/v1/machines')
        return createResponse({ machines: removed ? [] : [machine] })
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const showMachines = ref(true)
    const navigation = defineComponent({
      components: { MachinesView },
      setup: () => ({ showMachines }),
      template:
        '<KeepAlive><MachinesView v-if="showMachines" /><div v-else>另一页</div></KeepAlive>',
    })
    const wrapper = mountPage(navigation)
    await flushPromises()
    await wrapper.get('[aria-label="移除 一号机"]').trigger('click')
    await getButton(wrapper, '确认移除').trigger('click')
    expect(getButton(wrapper, '添加机器').attributes('disabled')).toBeDefined()
    expect(getButton(wrapper, '编辑机器').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[aria-label="移除 一号机"]').attributes('disabled')).toBeDefined()
    await getButton(wrapper, '添加机器').trigger('click')
    expect(wrapper.findAll('[role="dialog"]')).toHaveLength(1)
    expect(wrapper.get('[role="dialog"]').attributes('aria-label')).toBe('移除机器？')
    expect(wrapper.find('form').exists()).toBe(false)

    // 离页清空确认对象后，已提交请求仍按原机器编号完成。
    showMachines.value = false
    await flushPromises()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    removed = true
    deleteResponse.resolve(createResponse({}))
    await flushPromises()
    const readsBeforeReturn = requests.filter(
      (request) => request.url.pathname === '/api/v1/machines',
    ).length
    showMachines.value = true
    await flushPromises()
    expect(requests.filter((request) => request.url.pathname === '/api/v1/machines')).toHaveLength(
      readsBeforeReturn + 1,
    )
    expect(requests.filter((request) => request.options.method === 'DELETE')).toHaveLength(1)
    expect(wrapper.text()).toContain('机器已移除，已保存的测量记录仍可查询。')
    expect(wrapper.find('.equipment-card').exists()).toBe(false)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(getButton(wrapper, '添加机器').attributes('disabled')).toBeUndefined()
    await getButton(wrapper, '添加机器').trigger('click')
    expect(wrapper.get('[role="dialog"]').attributes('aria-label')).toBe('添加机器')
  })

  it('编辑字段错误保留草稿，删除必须经过页面确认且失败可重试', async () => {
    let removed = false
    let rejectDelete = true
    respond = (url, options) => {
      if (options.method === 'PUT') return createFailure('相机序列号重复', 400, 'camera_serial')
      if (options.method === 'DELETE') {
        if (rejectDelete) return createFailure('机器正在使用', 409)
        removed = true
        return createResponse({})
      }
      if (url.pathname === '/api/v1/machines')
        return createResponse({ machines: removed ? [] : [machine] })
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const wrapper = mountPage(MachinesView)
    await flushPromises()
    await getButton(wrapper, '编辑机器').trigger('click')
    await wrapper.findAll('[role="dialog"] input')[1].setValue('DUPLICATE')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('相机序列号重复')
    expect((wrapper.get('[aria-invalid="true"]').element as HTMLInputElement).value).toBe(
      'DUPLICATE',
    )
    await getButton(wrapper, '稍后继续').trigger('click')
    await getButton(wrapper, '编辑机器').trigger('click')
    expect((wrapper.findAll('[role="dialog"] input')[1].element as HTMLInputElement).value).toBe(
      'DUPLICATE',
    )
    await wrapper.get('[aria-label="关闭对话框"]').trigger('click')

    await wrapper.get('[aria-label="移除 一号机"]').trigger('click')
    expect(requests.some((request) => request.options.method === 'DELETE')).toBe(false)
    await getButton(wrapper, '保留机器').trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    await wrapper.get('[aria-label="移除 一号机"]').trigger('click')
    await getButton(wrapper, '确认移除').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('机器正在使用')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    rejectDelete = false
    await getButton(wrapper, '确认移除').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已保存的测量记录仍可查询')
    expect(wrapper.find('.equipment-card').exists()).toBe(false)
  })
})

describe('配置页面工作流', () => {
  it('校验只发送可编辑字段，保留脏草稿并显示后端字段错误', async () => {
    respond = (url) => {
      if (url.pathname === '/api/v1/machines') return createResponse({ machines: [machine] })
      if (url.pathname === '/api/v1/configuration/validate')
        return createFailure('频率下限必须小于上限', 400, 'minimum_frequency_hz')
      return createResponse({ settings, revision: 'revision-1' })
    }
    const wrapper = mountPage(ConfigurationView)
    await flushPromises()
    expect(getButton(wrapper, '保存配置').attributes('disabled')).toBeDefined()
    await wrapper.get('#minimum_frequency_hz').setValue('200')
    await wrapper.get('#camera_exposure_time_us').setValue('')
    await getButton(wrapper, '校验配置').trigger('click')
    await flushPromises()
    const validation = requests.find((request) => request.url.pathname.endsWith('/validate'))!
    const body = JSON.parse(validation.options.body as string)
    expect(validation.options.method).toBe('POST')
    expect(body.settings.minimum_frequency_hz).toBe(200)
    expect(body.settings.camera_exposure_time_us).toBeNull()
    expect(body.settings).not.toHaveProperty('database_path')
    expect(body).not.toHaveProperty('revision')
    expect(requests.some((request) => request.options.method === 'PUT')).toBe(false)
    expect(wrapper.get('#minimum_frequency_hz').attributes('aria-invalid')).toBe('true')
    expect(wrapper.text()).toContain('有未保存修改')
  })

  it('非有限配置显示原始文字，未修正时原样提交，修正后保存真实数值', async () => {
    respond = (url, options) => {
      if (url.pathname === '/api/v1/machines') return createResponse({ machines: [machine] })
      if (options.method === 'PUT') {
        const submitted = JSON.parse(options.body as string)
        if (submitted.settings.camera_gain === 'NaN')
          return createFailure('增益必须为有限数值', 400, 'camera_gain')
        return createResponse({
          settings: { ...settings, ...submitted.settings },
          revision: 'revision-2',
        })
      }
      return createResponse({
        settings: { ...settings, camera_gain: 'NaN' },
        revision: 'revision-1',
        invalid_fields: [{ field: 'camera_gain', raw_value: 'NaN', message: '增益必须为有限数值' }],
      })
    }
    const wrapper = mountPage(ConfigurationView)
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('camera_gain = NaN')
    expect(wrapper.get('#camera_gain').attributes('type')).toBe('text')
    expect(wrapper.get('#camera_gain').attributes('aria-invalid')).toBe('true')
    expect((wrapper.get('#camera_gain').element as HTMLInputElement).value).toBe('NaN')

    // 修改其他字段后提交，错误值必须保持原始字符串。
    await wrapper.get('#modbus_serial_port').setValue('COM8')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const invalidSave = requests.find((request) => request.options.method === 'PUT')!
    expect(JSON.parse(invalidSave.options.body as string).settings.camera_gain).toBe('NaN')
    expect((wrapper.get('#camera_gain').element as HTMLInputElement).value).toBe('NaN')
    expect(wrapper.text()).toContain('增益必须为有限数值')

    // 修正后恢复数值输入，保存载荷不再包含特殊字符串。
    await wrapper.get('#camera_gain').setValue('2.5')
    expect(wrapper.get('#camera_gain').attributes('type')).toBe('number')
    expect(wrapper.text()).not.toContain('camera_gain = NaN')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const validSave = requests.filter((request) => request.options.method === 'PUT').at(-1)!
    expect(JSON.parse(validSave.options.body as string).settings.camera_gain).toBe(2.5)
    expect(wrapper.get('#camera_gain').attributes('aria-invalid')).toBe('false')
    expect(wrapper.text()).toContain('配置已保存')
    expect(wrapper.text()).toContain('已与配置同步')
    expect(getButton(wrapper, '保存配置').attributes('disabled')).toBeDefined()
  })

  it('版本冲突保留草稿并阻止覆盖，确认重新读取后使用最新版本保存', async () => {
    let revision = 'revision-1'
    let conflict = true
    let savedSettings = { ...settings }
    respond = (url, options) => {
      if (url.pathname === '/api/v1/machines') return createResponse({ machines: [machine] })
      if (options.method === 'PUT') {
        if (conflict) return createFailure('配置已被其他进程修改', 409)
        savedSettings = { ...settings, ...JSON.parse(options.body as string).settings }
        return createResponse({ settings: savedSettings, revision: 'revision-3' })
      }
      return createResponse({ settings: savedSettings, revision })
    }
    const wrapper = mountPage(ConfigurationView)
    await flushPromises()
    await wrapper.get('#modbus_serial_port').setValue('COM9')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const firstSave = requests.find((request) => request.options.method === 'PUT')!
    expect(JSON.parse(firstSave.options.body as string).revision).toBe('revision-1')
    expect((wrapper.get('#modbus_serial_port').element as HTMLInputElement).value).toBe('COM9')
    expect(wrapper.text()).toContain('草稿仍保留')
    expect(getButton(wrapper, '保存配置').attributes('disabled')).toBeDefined()

    // 取消重新读取不丢弃本地草稿，也不发送读取请求。
    const readCount = requests.length
    await getButton(wrapper, '重新读取').trigger('click')
    await getButton(wrapper, '保留草稿').trigger('click')
    expect(requests).toHaveLength(readCount)
    expect((wrapper.get('#modbus_serial_port').element as HTMLInputElement).value).toBe('COM9')
    revision = 'revision-2'
    savedSettings.modbus_serial_port = 'COM4'
    await getButton(wrapper, '重新读取').trigger('click')
    await getButton(wrapper, '放弃草稿并读取').trigger('click')
    await flushPromises()
    expect((wrapper.get('#modbus_serial_port').element as HTMLInputElement).value).toBe('COM4')
    expect(wrapper.text()).toContain('已与配置同步')

    conflict = false
    await wrapper.get('#modbus_serial_port').setValue('COM7')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const lastSave = requests.filter((request) => request.options.method === 'PUT').at(-1)!
    expect(JSON.parse(lastSave.options.body as string).revision).toBe('revision-2')
    expect(wrapper.text()).toContain('配置已保存')
    expect(getButton(wrapper, '保存配置').attributes('disabled')).toBeDefined()
  })
})

describe('异常页面工作流', () => {
  it('使用服务端分页和完整周期筛选，复制原始 JSON 而非格式化展示', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const event: AbnormalEvent = {
      abnormal_event_id: 31,
      created_at: 1791021600,
      machine_id: '1',
      session_id: 'session/with spaces',
      reason: '设备通信超时',
      payload_json: '{"raw":  [1,2], "preserve":"空格"}',
      payload_summary: '串口超时',
    }
    respond = (url) => {
      if (url.pathname === '/api/v1/abnormal-events/machines')
        return createResponse({ machine_ids: ['1'] })
      if (url.pathname === '/api/v1/abnormal-events/31') return createResponse({ event })
      return createResponse({
        events: [event],
        page: Number(url.searchParams.get('page')),
        page_size: 20,
        total: 45,
        total_pages: 3,
      })
    }
    const wrapper = mountPage(AbnormalView)
    await flushPromises()
    await wrapper.get('[aria-label="完整 Session ID"]').setValue('session/with spaces')
    await wrapper.get('[aria-label="异常机器"]').setValue('1')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('[aria-label="下一页"]').trigger('click')
    await flushPromises()
    const latest = requests
      .filter((request) => request.url.pathname === '/api/v1/abnormal-events')
      .at(-1)!
    expect(Object.fromEntries(latest.url.searchParams)).toMatchObject({
      page: '2',
      page_size: '20',
      session_id: 'session/with spaces',
      machine_id: '1',
    })
    expect(wrapper.text()).toContain('第 2 / 3 页')
    await wrapper.get('[aria-label="查看异常事件 31"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('pre').text()).toBe(JSON.stringify(JSON.parse(event.payload_json), null, 2))
    await getButton(wrapper, '复制原始 JSON').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenLastCalledWith(event.payload_json)
    expect(wrapper.text()).toContain('原始 JSON已复制')
    await getButton(wrapper, '复制 Session ID').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenLastCalledWith(event.session_id)
  })

  it('复制失败显示错误，损坏 JSON 原样展示', async () => {
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('剪贴板被拒绝')) },
    })
    const event: AbnormalEvent = {
      abnormal_event_id: 8,
      created_at: 1791021600,
      machine_id: '1',
      session_id: null,
      reason: '无法解析',
      payload_json: '{broken payload',
    }
    respond = (url) => {
      if (url.pathname.endsWith('/machines')) return createResponse({ machine_ids: ['1'] })
      if (url.pathname.endsWith('/8')) return createResponse({ event })
      return createResponse({ events: [event], page: 1, page_size: 20, total: 1, total_pages: 1 })
    }
    const wrapper = mountPage(AbnormalView)
    await flushPromises()
    await wrapper.get('[aria-label="查看异常事件 8"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('pre').text()).toBe(event.payload_json)
    expect(wrapper.text()).not.toContain('复制 Session ID')
    await getButton(wrapper, '复制原始 JSON').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('剪贴板被拒绝')
  })
})

describe('图片页面工作流', () => {
  it('卡片进入视野才读取预览目录，进入详情才读取完整证据列表', async () => {
    const record = createRecord('lazy-record')
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage([record])
      if (url.pathname === '/api/v1/records/lazy-record') return createResponse({ record })
      if (url.pathname.endsWith('/evidence'))
        return createResponse({
          images: [],
          state: 'missing_directory',
          count: null,
          page: 1,
          page_size: Number(url.searchParams.get('page_size')),
          total_pages: 1,
        })
      return defaultRespond(url, options)
    }
    const wrapper = mountPage(ImagesView)
    await flushPromises()
    expect(
      requests
        .find((request) => request.url.pathname === '/api/v1/records')
        ?.url.searchParams.get('page_size'),
    ).toBe('12')
    expect(requests.some((request) => request.url.pathname.endsWith('/evidence'))).toBe(false)
    expect(wrapper.text()).toContain('进入视野后读取')
    observeCallbacks[0]()
    await flushPromises()
    let evidenceRequests = requests.filter((request) => request.url.pathname.endsWith('/evidence'))
    expect(evidenceRequests).toHaveLength(1)
    expect(evidenceRequests[0].url.searchParams.get('page_size')).toBe('1')
    expect(wrapper.text()).toContain('证据目录不存在')

    await wrapper.get('.evidence-card').trigger('click')
    await flushPromises()
    evidenceRequests = requests.filter((request) => request.url.pathname.endsWith('/evidence'))
    expect(evidenceRequests).toHaveLength(2)
    expect(evidenceRequests[1].url.searchParams.get('page_size')).toBe('24')
    expect(wrapper.get('[role="dialog"]').text()).toContain('证据目录不存在')
  })

  it('完整图片按选择鉴权读取，切图和关闭详情释放对象地址', async () => {
    const createObjectURL = vi
      .fn()
      .mockReturnValueOnce('blob:first-image')
      .mockReturnValueOnce('blob:second-image')
    const revokeObjectURL = vi.fn()
    const NativeURL = URL
    vi.stubGlobal(
      'URL',
      class extends NativeURL {
        static createObjectURL = createObjectURL
        static revokeObjectURL = revokeObjectURL
      },
    )
    const record = createRecord('image-record')
    const images = [
      {
        image_id: 'first',
        filename: 'first.jpg',
        url: '/api/v1/records/image-record/images/first',
        thumbnail_url: '/api/v1/records/image-record/images/first/thumbnail',
      },
      {
        image_id: 'second',
        filename: 'second.jpg',
        url: '/api/v1/records/image-record/images/second',
        thumbnail_url: '/api/v1/records/image-record/images/second/thumbnail',
      },
    ]
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage([record])
      if (url.pathname === '/api/v1/records/image-record') return createResponse({ record })
      if (url.pathname.endsWith('/evidence'))
        return createResponse({
          images,
          state: 'available',
          count: 2,
          page: 1,
          page_size: 24,
          total_pages: 1,
        })
      if (url.pathname.includes('/images/'))
        return new Response(new Blob(['image bytes'], { type: 'image/jpeg' }))
      return defaultRespond(url, options)
    }
    const wrapper = mountPage(ImagesView)
    await flushPromises()
    await wrapper.get('.evidence-card').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="dialog"] img').attributes('src')).toBe('blob:first-image')
    expect(wrapper.get('[role="dialog"] img').attributes('alt')).toBe('first.jpg')
    const firstRead = requests.find((request) => request.url.pathname.endsWith('/images/first'))!
    expect(firstRead.options.headers).toMatchObject({ Authorization: 'Bearer workflow-token' })
    expect(firstRead.url.search).toBe('')
    expect(requests.some((request) => request.url.pathname.endsWith('/images/second'))).toBe(false)

    // 切换原图后释放旧地址，关闭详情后释放最后一张原图。
    await wrapper.get('[aria-label="下一张图片"]').trigger('click')
    await flushPromises()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:first-image')
    expect(wrapper.get('[role="dialog"] img').attributes('alt')).toBe('second.jpg')
    expect(wrapper.get('[aria-label="下一张图片"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="关闭对话框"]').trigger('click')
    await flushPromises()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:second-image')
    expect(createObjectURL).toHaveBeenCalledTimes(2)
  })

  it('关闭详情取消请求，迟到旧记录不能覆盖新选中的周期', async () => {
    const oldResponse = createDeferredResponse()
    const records = [createRecord('old-record'), createRecord('new-record')]
    const defaultRespond = respond
    respond = (url, options) => {
      if (url.pathname === '/api/v1/records') return createRecordPage(records)
      if (url.pathname === '/api/v1/records/old-record') return oldResponse.promise
      if (url.pathname === '/api/v1/records/new-record')
        return createResponse({ record: records[1] })
      if (url.pathname.endsWith('/evidence'))
        return createResponse({
          images: [],
          state: 'no_jpg',
          count: 0,
          page: 1,
          page_size: 24,
          total_pages: 1,
        })
      return defaultRespond(url, options)
    }
    const wrapper = mountPage(ImagesView)
    await flushPromises()
    await wrapper.findAll('.evidence-card')[0].trigger('click')
    const pendingRequest = requests.find((request) => request.url.pathname.endsWith('/old-record'))!
    await wrapper.get('[aria-label="关闭对话框"]').trigger('click')
    expect(pendingRequest.options.signal?.aborted).toBe(true)
    await wrapper.findAll('.evidence-card')[1].trigger('click')
    await flushPromises()
    oldResponse.resolve(createResponse({ record: records[0] }))
    await flushPromises()
    expect(wrapper.get('[role="dialog"] .session-caption').text()).toBe('new-record')
    expect(
      requests.some((request) => request.url.pathname === '/api/v1/records/old-record/evidence'),
    ).toBe(false)
    expect(
      requests.some((request) => request.url.pathname === '/api/v1/records/new-record/evidence'),
    ).toBe(true)
  })
})

/**
 * 创建用于实时排版和选择测试的机器快照。
 * Args:
 *   无外部参数。
 * Returns:
 *   Snapshot // 包含两台启用机器、一台停用机器和独立周期结果
 */
function createLayoutSnapshot(): Snapshot {
  return {
    status: 'stopped',
    running: false,
    failure: '',
    started_at: null,
    sequence: 10,
    machines: [
      {
        ...machine,
        id: '1',
        camera_state: '相机已连接',
        camera_error: '',
        status: 'online',
        warning: '',
        active_session_id: null,
        waiting_cycle_reset: false,
        inflight_count: 0,
      },
      {
        ...machine,
        id: '2',
        machine_name: '二号机',
        camera_serial: 'CAM-2',
        frequency_meter_serial: 'FREQ-2',
        camera_state: '连接失败',
        camera_error: '相机连接中断',
        status: 'fault',
        warning: '',
        active_session_id: null,
        waiting_cycle_reset: false,
        inflight_count: 0,
      },
      {
        ...machine,
        id: '3',
        machine_name: '停用机器',
        enabled: false,
        camera_state: '未连接',
        camera_error: '',
        status: 'online',
        warning: '',
        active_session_id: null,
        waiting_cycle_reset: false,
        inflight_count: 0,
      },
    ],
    sessions: [
      {
        machine_id: '1',
        session_id: 'first-cycle',
        state: 'COMMITTED',
        cycle_closed: true,
        stages: {
          session_start: 'success',
          image_capture: 'success',
          frequency_collection: 'success',
          character_recognition: 'success',
          evidence_storage: 'success',
        },
        recognized_lines: ['ABCDEFGHIJKLMNOPQRST', '1234567A', '1234568A', '123', '45'],
        final_frequency_hz: 52.5,
        start_time: null,
        finish_time: null,
        errors: [],
      },
      {
        machine_id: '2',
        session_id: 'second-cycle',
        state: 'FAILED',
        cycle_closed: true,
        stages: { session_start: 'success', image_capture: 'failed' },
        recognized_lines: ['9876543B'],
        final_frequency_hz: null,
        start_time: null,
        finish_time: null,
        errors: ['采集失败'],
      },
    ],
  }
}

describe('实时页面排版与刷新', () => {
  it('按原分组组织两张总览、机器卡片和右侧详情，选择时整组更新', async () => {
    runtime.value = createLayoutSnapshot()
    respond = () => createResponse({ recognition_count: 24, pending_review_count: 3 })
    const wrapper = mountPage(RealtimeView)
    await flushPromises()

    // 总览保留三项设备指标和两项今日指标。
    const summaries = wrapper.findAll('.metrics-grid > article')
    expect(summaries).toHaveLength(2)
    expect(summaries.map((summary) => summary.get('h2').text())).toEqual(['设备总览', '今日检测'])
    expect(summaries[0].findAll('dt').map((item) => item.text())).toEqual([
      '总机器',
      '在线机器',
      '故障机器',
    ])
    expect(summaries[0].findAll('dd').map((item) => item.text())).toEqual(['2', '1', '1'])
    expect(summaries[1].findAll('dd').map((item) => item.text())).toEqual(['24', '3'])
    expect(wrapper.find('.operation-bar').exists()).toBe(false)

    // 操作栏、机器列表和详情是同一工作区内的独立区域。
    expect(wrapper.findAll('.machine-section-heading button').map((item) => item.text())).toEqual([
      '刷新',
      '停止监测',
      '启动监测',
    ])
    expect(wrapper.find('.machine-workspace > .machine-scroll > .machine-grid').exists()).toBe(true)
    expect(wrapper.find('.machine-workspace > .detail-scroll > .session-detail').exists()).toBe(
      true,
    )
    const cards = wrapper.findAll('.machine-card')
    expect(cards).toHaveLength(3)
    expect(cards[0].get('.camera-status').text()).toBe('相机已连接')
    expect(cards[0].get('.machine-metrics').text()).toContain('52.5 Hz')
    expect(cards[0].get('.machine-ocr-summary code').text()).toBe('ABCDEFGHIJKLMNOPQRST')

    // 详情按元数据、频率、分类识别和五步进度排列。
    const detail = wrapper.get('.session-detail')
    const sectionOrder = Array.from(detail.element.children).map((child) => child.className)
    expect(sectionOrder.slice(0, 6)).toEqual([
      'section-heading compact',
      'machine-attributes',
      'detail-frequency',
      'detail-ocr',
      'detail-progress',
      'detail-stats',
    ])
    expect(detail.findAll('.machine-attributes dt').map((item) => item.text())).toEqual([
      '相机序列号',
      '频率仪序列号',
      '本轮流程',
      '相机状态',
    ])
    expect(detail.get('.ocr-result').text()).toBe(
      '20  ABCDEFGHIJKLMNOPQRST\n8  1234567A\n    1234568A\n3  123\n2  45',
    )
    expect(detail.findAll('.stage-list li')).toHaveLength(5)
    expect(detail.get('.stage-list li').attributes('aria-label')).toBe('本轮启动：已完成')
    expect(detail.findAll('.detail-stats dd').map((item) => item.text())).toEqual(['—', '—', '—'])

    // 切换机器后不保留上一台机器的结果或状态。
    await cards[1].trigger('click')
    expect(cards[1].attributes('aria-pressed')).toBe('true')
    expect(detail.get('h2').text()).toBe('二号机')
    expect(detail.text()).toContain('CAM-2')
    expect(detail.text()).toContain('FREQ-2')
    expect(detail.text()).toContain('second-cycle')
    expect(detail.get('.ocr-result').text()).toBe('20  --\n8  9876543B\n3  --\n2  --')
    expect(detail.text()).not.toContain('ABCDEFGHIJKLMNOPQRST')
    expect(detail.get('.notice').text()).toContain('相机连接中断')
    expect(detail.findAll('.stage-list li')[1].attributes('aria-label')).toBe('图像采集：失败')
  })

  it('刷新并行读取快照与统计，迟到的旧快照不能覆盖 SSE 新状态', async () => {
    runtime.value = createLayoutSnapshot()
    const delayedState = createDeferredResponse()
    let summaryCount = 0
    respond = (url) => {
      if (url.pathname === '/api/v1/state') return delayedState.promise
      if (url.pathname === '/api/v1/records/summary')
        return createResponse({ recognition_count: ++summaryCount, pending_review_count: 0 })
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    await getButton(wrapper, '刷新').trigger('click')
    expect(getButton(wrapper, '刷新').attributes('disabled')).toBeDefined()
    await flushPromises()
    expect(summaryCount).toBe(2)
    runtime.value = {
      ...runtime.value,
      sequence: 12,
      machines: runtime.value.machines.map((item) => ({ ...item, machine_name: '实时新名称' })),
    }
    delayedState.resolve(createResponse({ ...createLayoutSnapshot(), sequence: 11 }))
    await flushPromises()
    expect(runtime.value.sequence).toBe(12)
    expect(wrapper.get('.session-detail h2').text()).toBe('实时新名称')
    expect(getButton(wrapper, '刷新').attributes('disabled')).toBeUndefined()
  })

  it('快照立即应用，统计仍在读取时禁用刷新，离页后不写入保留的旧结果', async () => {
    runtime.value = createLayoutSnapshot()
    const delayedState = createDeferredResponse()
    const delayedSummary = createDeferredResponse()
    let summaryCount = 0
    respond = (url) => {
      if (url.pathname === '/api/v1/state') return delayedState.promise
      if (url.pathname === '/api/v1/records/summary') {
        summaryCount++
        return summaryCount === 1
          ? createResponse({ recognition_count: 1, pending_review_count: 0 })
          : delayedSummary.promise
      }
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const showRealtime = ref(true)
    const host = defineComponent({
      components: { RealtimeView },
      setup: () => ({ showRealtime }),
      template: '<KeepAlive><RealtimeView v-if="showRealtime" /></KeepAlive>',
    })
    const wrapper = mountPage(host)
    await flushPromises()
    await getButton(wrapper, '刷新').trigger('click')

    // 快照完成时立即更新机器，不等待较慢的统计接口。
    delayedState.resolve(createResponse({ ...createLayoutSnapshot(), sequence: 11 }))
    await flushPromises()
    expect(runtime.value.sequence).toBe(11)
    expect(getButton(wrapper, '刷新').attributes('disabled')).toBeDefined()

    // 页面离开后，旧读取不能覆盖其他连接重建的低序号快照。
    showRealtime.value = false
    await nextTick()
    runtime.value = { ...createLayoutSnapshot(), sequence: 1 }
    delayedSummary.resolve(createResponse({ recognition_count: 999, pending_review_count: 999 }))
    await flushPromises()
    expect(runtime.value.sequence).toBe(1)
  })

  it('刷新途中断线再重连时丢弃旧服务返回的高序号快照', async () => {
    runtime.value = createLayoutSnapshot()
    const delayedState = createDeferredResponse()
    respond = (url) =>
      url.pathname === '/api/v1/state'
        ? delayedState.promise
        : createResponse({ recognition_count: 1, pending_review_count: 0 })
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    await getButton(wrapper, '刷新').trigger('click')
    connection.value = 'reconnecting'
    connection.value = 'connected'
    runtime.value = { ...createLayoutSnapshot(), sequence: 1 }
    delayedState.resolve(createResponse({ ...createLayoutSnapshot(), sequence: 100 }))
    await flushPromises()
    expect(runtime.value.sequence).toBe(1)
    expect(getButton(wrapper, '刷新').attributes('disabled')).toBeUndefined()
  })

  it('统计刷新失败时明确提示，不把旧数字当成本次成功读取', async () => {
    runtime.value = createLayoutSnapshot()
    let summaryCount = 0
    respond = (url) => {
      if (url.pathname === '/api/v1/state') return createResponse(createLayoutSnapshot())
      if (url.pathname === '/api/v1/records/summary') {
        summaryCount++
        return summaryCount === 1
          ? createResponse({ recognition_count: 24, pending_review_count: 3 })
          : createFailure('今日统计读取失败', 503)
      }
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    await getButton(wrapper, '刷新').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('今日统计读取失败')
    expect(
      wrapper
        .findAll('.metrics-grid > article')[1]
        .findAll('dd')
        .map((item) => item.text()),
    ).toEqual(['24', '3'])
    expect(getButton(wrapper, '刷新').attributes('disabled')).toBeUndefined()
  })

  it('刷新失败保留当前展示并报告错误，断线时禁用刷新', async () => {
    runtime.value = createLayoutSnapshot()
    respond = (url) =>
      url.pathname === '/api/v1/state'
        ? createFailure('快照读取失败', 503)
        : createResponse({ recognition_count: 1, pending_review_count: 0 })
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    await getButton(wrapper, '刷新').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('快照读取失败')
    expect(wrapper.get('.session-detail h2').text()).toBe('一号机')
    connection.value = 'reconnecting'
    await nextTick()
    expect(getButton(wrapper, '刷新').attributes('disabled')).toBeDefined()
  })

  it('无机器时保留总览、操作栏和详情占位', async () => {
    runtime.value = { ...createLayoutSnapshot(), machines: [], sessions: [] }
    respond = () => createResponse({ recognition_count: 0, pending_review_count: 0 })
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    expect(wrapper.findAll('.metrics-grid > article')).toHaveLength(2)
    expect(wrapper.findAll('.machine-section-heading button')).toHaveLength(3)
    expect(wrapper.get('.machine-scroll').text()).toContain('尚未配置机器')
    expect(wrapper.get('.session-detail h2').text()).toBe('未选择机器')
    expect(wrapper.get('.ocr-result').text()).toBe('20  --\n8  --\n3  --\n2  --')
    expect(wrapper.find('.session-id').exists()).toBe(false)
  })
})

describe('实时页面工作流', () => {
  it('新周期不泄露旧结果，断线阻止控制并暂停本地动画', async () => {
    const snapshot: Snapshot = {
      status: 'running',
      running: true,
      failure: '',
      started_at: null,
      sequence: 4,
      machines: [
        {
          id: '1',
          machine_name: '一号机',
          camera_serial: 'CAM-1',
          frequency_meter_serial: 'FREQ-1',
          enabled: true,
          camera_state: 'capturing',
          camera_error: '',
          status: 'online',
          warning: '',
          active_session_id: 'new-cycle',
          waiting_cycle_reset: false,
          inflight_count: 1,
        },
      ],
      sessions: [
        {
          machine_id: '1',
          session_id: 'old-cycle',
          state: 'COMMITTED',
          cycle_closed: true,
          stages: { image_capture: 'success' },
          recognized_lines: ['OLD-RESULT'],
          final_frequency_hz: 99,
          start_time: null,
          finish_time: null,
          errors: [],
        },
        {
          machine_id: '1',
          session_id: 'new-cycle',
          state: 'RUNNING',
          cycle_closed: false,
          stages: { image_capture: 'running', frequency_collection: 'running' },
          recognized_lines: [],
          final_frequency_hz: null,
          start_time: null,
          finish_time: null,
          errors: [],
        },
      ],
    }
    runtime.value = snapshot
    respond = (url) => {
      if (url.pathname === '/api/v1/records/summary')
        return createResponse({ recognition_count: 24, pending_review_count: 3 })
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    expect(wrapper.get('.session-detail').text()).toContain('new-cycle')
    expect(wrapper.get('.session-detail').text()).not.toContain('OLD-RESULT')
    expect(wrapper.get('.frequency-value').text()).toBe('—Hz')
    expect(wrapper.get('.belt-animation').attributes('aria-label')).toContain('相机采集中')
    connection.value = 'reconnecting'
    await nextTick()
    expect(getButton(wrapper, '停止监测').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('当前为最后一次同步状态')
    expect(wrapper.findComponent({ name: 'BeltAnimation' }).props('paused')).toBe(true)
  })

  it('停止监测需要确认，并以服务端快照显示最终状态', async () => {
    runtime.value = {
      status: 'running',
      running: true,
      failure: '',
      started_at: null,
      sequence: 5,
      machines: [],
      sessions: [],
    }
    const stopResponse = createDeferredResponse()
    respond = (url) => {
      if (url.pathname === '/api/v1/records/summary')
        return createResponse({ recognition_count: 0, pending_review_count: 0 })
      if (url.pathname === '/api/v1/monitoring/stop') return stopResponse.promise
      throw new Error(`未配置接口：${url.pathname}`)
    }
    const wrapper = mountPage(RealtimeView)
    await flushPromises()
    await getButton(wrapper, '停止监测').trigger('click')
    expect(requests.some((request) => request.url.pathname.endsWith('/stop'))).toBe(false)
    await getButton(wrapper, '继续监测').trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    await getButton(wrapper, '停止监测').trigger('click')
    await wrapper.get('[role="dialog"] .button.danger').trigger('click')
    expect(runtime.value.running).toBe(true)
    expect(getButton(wrapper, '正在停止').attributes('disabled')).toBeDefined()
    stopResponse.resolve(
      createResponse({ ...runtime.value, status: 'stopped', running: false, sequence: 6 }),
    )
    await flushPromises()
    expect(runtime.value.running).toBe(false)
    expect(getButton(wrapper, '启动监测').attributes('disabled')).toBeUndefined()
    expect(requests.filter((request) => request.url.pathname.endsWith('/stop'))).toHaveLength(1)
  })
})
