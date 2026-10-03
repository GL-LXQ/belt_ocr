import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, KeepAlive, ref } from 'vue'
import AuthenticatedImage from './AuthenticatedImage.vue'
import { configureApi } from '../lib/api'

describe('鉴权图片生命周期', () => {
  const wrappers: ReturnType<typeof mount>[] = []
  const createObjectURL = vi.fn(() => 'blob:current-image')
  const revokeObjectURL = vi.fn()
  beforeEach(() => {
    configureApi('', 'private-token')
    createObjectURL.mockClear()
    revokeObjectURL.mockClear()
    vi.stubGlobal(
      'URL',
      class extends URL {
        static createObjectURL = createObjectURL
        static revokeObjectURL = revokeObjectURL
      },
    )
  })
  afterEach(() => {
    wrappers.forEach((wrapper) => wrapper.unmount())
    wrappers.length = 0
    vi.unstubAllGlobals()
  })

  it('快速切图时忽略迟到的旧图片并释放对象地址', async () => {
    const responses: ((response: Response) => void)[] = []
    const fetch = vi.fn(() => new Promise<Response>((resolve) => responses.push(resolve)))
    vi.stubGlobal('fetch', fetch)
    const wrapper = mount(AuthenticatedImage, { props: { path: '/api/v1/first', alt: 'first' } })
    wrappers.push(wrapper)
    await wrapper.setProps({ path: '/api/v1/second', alt: 'second' })
    expect((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].signal?.aborted).toBe(true)
    responses[1](new Response(new Blob(['second'])))
    await flushPromises()
    responses[0](new Response(new Blob(['first'])))
    await flushPromises()
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(wrapper.get('img').attributes('alt')).toBe('second')
    await wrapper.setProps({ active: false })
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:current-image')
    expect(wrapper.find('img').exists()).toBe(false)
  })

  it('卸载后完成的请求不能创建对象地址', async () => {
    let resolve!: (response: Response) => void
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((finish) => {
            resolve = finish
          }),
      ),
    )
    const wrapper = mount(AuthenticatedImage, { props: { path: '/api/v1/image', alt: 'image' } })
    wrapper.unmount()
    resolve(new Response(new Blob(['late'])))
    await flushPromises()
    expect(createObjectURL).not.toHaveBeenCalled()
  })

  it('KeepAlive 隐藏时释放图片，返回后重新读取', async () => {
    const fetch = vi
      .fn()
      .mockImplementation(() => Promise.resolve(new Response(new Blob(['image']))))
    vi.stubGlobal('fetch', fetch)
    const visible = ref(true)
    const parent = defineComponent({
      setup: () => () =>
        h(
          KeepAlive,
          {},
          {
            default: () =>
              visible.value ? h(AuthenticatedImage, { path: '/api/v1/image', alt: 'image' }) : null,
          },
        ),
    })
    const wrapper = mount(parent)
    wrappers.push(wrapper)
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
    visible.value = false
    await flushPromises()
    expect(revokeObjectURL).toHaveBeenCalledTimes(1)
    visible.value = true
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('卡片图片读取失败不会生成嵌套重试按钮', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 404 })))
    const wrapper = mount(AuthenticatedImage, {
      props: { path: '/api/v1/missing', alt: 'missing', retryable: false },
    })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('404')
    expect(wrapper.find('button').exists()).toBe(false)
  })
})
