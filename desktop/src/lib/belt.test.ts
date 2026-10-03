import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, KeepAlive, nextTick, ref } from 'vue'
import BeltAnimation from '../components/BeltAnimation.vue'
import { advanceBeltState, easeExtension, renderBeltSvg, type BeltVisualState } from './belt'

const stoppedState: BeltVisualState = {
  extension: 0,
  capturing: false,
  frequencyListening: false,
  travel: 0,
  scanPhase: 0,
  frequencyPhase: 0,
}

/**
 * 解析代码生成的 SVG 元素。
 * Args:
 *   state: 当前帧的视觉参数。
 *   instanceId: 测试实例标识。
 * Returns:
 *   SVGElement // 已解析的 SVG 根元素
 */
function parseScene(state = stoppedState, instanceId = 'test'): Element {
  return new DOMParser().parseFromString(renderBeltSvg(state, instanceId), 'image/svg+xml')
    .documentElement
}

/**
 * 读取两端滚筒的中心横坐标。
 * Args:
 *   wrapper: 当前组件测试实例。
 * Returns:
 *   [315, 645] // 收起状态的两个滚筒横坐标
 */
function readRollerPositions(wrapper: VueWrapper): number[] {
  return wrapper
    .findAll('ellipse')
    .filter((ellipse) => ellipse.attributes('fill')?.endsWith('-rim)'))
    .map((ellipse) => Number(ellipse.attributes('cx')))
}

describe('原始皮带 SVG 几何和材质', () => {
  it('保留收紧画布、透明背景、完整机械元素和文字', () => {
    const scene = parseScene()
    expect(scene.getAttribute('viewBox')).toBe('90 46 844 402')
    expect(scene.getAttribute('preserveAspectRatio')).toBe('xMidYMid meet')
    expect(scene.querySelectorAll('*')).toHaveLength(416)
    expect(scene.querySelectorAll('linearGradient, radialGradient')).toHaveLength(12)
    expect(scene.textContent).toContain('BELT  /  VISION')
    expect(scene.textContent).toContain('VISION')
    expect(scene.querySelector('image, script, foreignObject, filter')).toBeNull()
  })

  it('展开时保留原始两端距离和滚筒半径', () => {
    const collapsed = parseScene()
    const extended = parseScene({ ...stoppedState, extension: 1 })
    const collapsedRims = [...collapsed.querySelectorAll('ellipse')].filter(
      (ellipse) => ellipse.getAttribute('fill') === 'url(#test-rim)',
    )
    const extendedRims = [...extended.querySelectorAll('ellipse')].filter(
      (ellipse) => ellipse.getAttribute('fill') === 'url(#test-rim)',
    )
    expect(collapsedRims.map((ellipse) => Number(ellipse.getAttribute('cx')))).toEqual([315, 645])
    expect(extendedRims.map((ellipse) => Number(ellipse.getAttribute('cx')))).toEqual([200, 760])
    expect(extendedRims.map((ellipse) => Number(ellipse.getAttribute('rx')))).toEqual([30, 62])
    expect(extended.querySelectorAll('*')).toHaveLength(452)
  })

  it('不同组件的全部渐变引用独立且不接收外部 SVG 标记', () => {
    const first = parseScene(stoppedState, 'first')
    const second = parseScene(stoppedState, 'second')
    const firstIds = [...first.querySelectorAll('[id]')].map((element) => element.id)
    const secondIds = [...second.querySelectorAll('[id]')].map((element) => element.id)
    expect(firstIds.every((id) => !secondIds.includes(id))).toBe(true)
    for (const element of first.querySelectorAll('[fill]')) {
      const fill = element.getAttribute('fill') ?? ''
      if (fill.startsWith('url(#')) expect(firstIds).toContain(fill.slice(5, -1))
    }
    const unsafe = parseScene(stoppedState, '\"><script>alert(1)</script>')
    expect(unsafe.tagName).toBe('svg')
    expect(unsafe.querySelector('script, parsererror')).toBeNull()
  })

  it('相机光束和频率光束可以各自独立显示', () => {
    const captureOnly = parseScene({ ...stoppedState, capturing: true })
    const frequencyOnly = parseScene({
      ...stoppedState,
      frequencyListening: true,
    })
    expect(captureOnly.querySelector('[fill="url(#test-beam)"]')).not.toBeNull()
    expect(captureOnly.querySelector('[stroke="#ee8979"]')).toBeNull()
    expect(frequencyOnly.querySelector('[fill="url(#test-beam)"]')).toBeNull()
    expect(frequencyOnly.querySelector('[stroke="#ee8979"]')).not.toBeNull()
  })

  it('旋转孔位按原始半径与皮带累计位移改变', () => {
    const scene = parseScene({ ...stoppedState, travel: (34 * Math.PI) / 2 })
    const firstHole = scene.querySelector('[fill="#5d6c79"]')
    expect(Number(firstHole?.getAttribute('cx'))).toBeCloseTo(315, 3)
    expect(Number(firstHole?.getAttribute('cy'))).toBeCloseTo(278.9, 3)
  })
})

describe('本地动画时间', () => {
  it('完整保留 Qt InOutCubic 的展开曲线', () => {
    expect([-1, 0, 0.25, 0.5, 0.75, 1, 2].map(easeExtension)).toEqual([
      0, 0, 0.0625, 0.5, 0.9375, 1, 1,
    ])
  })

  it('三十毫秒对应原始滚动、扫描和传感器速度', () => {
    const state = advanceBeltState(
      { ...stoppedState, capturing: true, frequencyListening: true },
      30,
      true,
    )
    expect(state.travel).toBeCloseTo(2.8, 12)
    expect(state.scanPhase).toBeCloseTo(0.025 * Math.PI * 2, 12)
    expect(state.frequencyPhase).toBeCloseTo(0.15, 12)
    expect(stoppedState.travel).toBe(0)
  })

  it('未启用的独立相位不会推进', () => {
    const state = advanceBeltState({ ...stoppedState, capturing: true }, 60, false)
    expect(state.travel).toBe(0)
    expect(state.scanPhase).toBeCloseTo(0.05 * Math.PI * 2, 12)
    expect(state.frequencyPhase).toBe(0)
    expect(advanceBeltState(stoppedState, 500, false)).toEqual(stoppedState)
  })
})

describe('Vue 本地动画生命周期', () => {
  let frameIndex = 0
  let frameCallbacks: Map<number, FrameRequestCallback>
  let motionListeners: Set<() => void>
  let reducedMotion: boolean
  let hidden: boolean
  let mountedWrappers: VueWrapper[]

  beforeEach(() => {
    frameIndex = 0
    frameCallbacks = new Map()
    motionListeners = new Set()
    reducedMotion = false
    hidden = false
    mountedWrappers = []
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frameCallbacks.set(++frameIndex, callback)
      return frameIndex
    })
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => frameCallbacks.delete(handle))
    vi.stubGlobal('matchMedia', () => ({
      get matches() {
        return reducedMotion
      },
      addEventListener: (_type: string, listener: () => void) => motionListeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) =>
        motionListeners.delete(listener),
    }))
  })

  afterEach(() => {
    for (const wrapper of mountedWrappers) wrapper.unmount()
    vi.unstubAllGlobals()
  })

  /**
   * 创建指定业务状态的动画组件。
   * Args:
   *   props: 初始业务开关。
   * Returns:
   *   VueWrapper // 当前组件实例
   */
  function mountAnimation(
    props: Partial<{
      extended: boolean
      capturing: boolean
      frequencyListening: boolean
      paused: boolean
    }> = {},
  ): VueWrapper {
    const wrapper = mount(BeltAnimation, {
      props: {
        extended: false,
        capturing: false,
        frequencyListening: false,
        ...props,
      },
    })
    mountedWrappers.push(wrapper)
    return wrapper
  }

  /**
   * 执行指定时刻的所有浏览器动画回调。
   * Args:
   *   timestamp: 当前测试帧的毫秒时间。
   * Returns:
   *   Promise<void> // Vue 已完成当前帧更新
   */
  async function advanceBrowserFrame(timestamp: number): Promise<void> {
    const callbacks = [...frameCallbacks.values()]
    frameCallbacks.clear()
    for (const callback of callbacks) callback(timestamp)
    await nextTick()
  }

  it('空闲时不刷新，展开过程使用六百五十毫秒且保留中心', async () => {
    const wrapper = mountAnimation()
    expect(frameCallbacks.size).toBe(0)
    await wrapper.setProps({ extended: true })
    await advanceBrowserFrame(0)
    await advanceBrowserFrame(325)
    expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])
    await advanceBrowserFrame(650)
    expect(readRollerPositions(wrapper)).toEqual([200, 760])
    expect(frameCallbacks.size).toBe(1)
  })

  it('收缩途中重新启动时从当前几何位置继续展开', async () => {
    const wrapper = mountAnimation({ extended: true })
    await advanceBrowserFrame(0)
    await advanceBrowserFrame(650)
    await wrapper.setProps({ extended: false })
    await advanceBrowserFrame(975)
    expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])
    await wrapper.setProps({ extended: true })
    expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])
    await advanceBrowserFrame(1625)
    expect(readRollerPositions(wrapper)).toEqual([200, 760])
  })

  it('暂停和浏览器后台不会继续刷新或累计隐藏时长', async () => {
    const wrapper = mountAnimation({ extended: true })
    await advanceBrowserFrame(0)
    await advanceBrowserFrame(325)
    await wrapper.setProps({ paused: true })
    expect(frameCallbacks.size).toBe(0)
    await wrapper.setProps({ paused: false })
    await advanceBrowserFrame(10000)
    expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    expect(frameCallbacks.size).toBe(0)
    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    await advanceBrowserFrame(20000)
    expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])
    await advanceBrowserFrame(20325)
    expect(readRollerPositions(wrapper)).toEqual([200, 760])
  })

  it('减少动态效果时展示静态运行状态，偏好改变后可恢复', async () => {
    reducedMotion = true
    const wrapper = mountAnimation({
      extended: true,
      capturing: true,
      frequencyListening: true,
    })
    await nextTick()
    expect(readRollerPositions(wrapper)).toEqual([200, 760])
    expect(frameCallbacks.size).toBe(0)
    expect(wrapper.attributes('aria-label')).toContain('相机采集中')
    expect(wrapper.find('[stroke="#ee8979"]').exists()).toBe(true)
    reducedMotion = false
    for (const listener of motionListeners) listener()
    expect(frameCallbacks.size).toBe(1)
  })

  it('缓存路由离开时停止刷新，返回时保留滚动和独立子动画相位', async () => {
    const visible = ref(true)
    const cachedScene = defineComponent({
      setup: () => () =>
        h(BeltAnimation, {
          extended: true,
          capturing: true,
          frequencyListening: true,
        }),
    })
    const wrapper = mount(
      defineComponent({
        setup: () => () =>
          h(KeepAlive, null, {
            default: () => (visible.value ? h(cachedScene) : null),
          }),
      }),
    )
    mountedWrappers.push(wrapper)

    // 推进至运行状态并记录当前完整几何与光束相位。
    await advanceBrowserFrame(0)
    await advanceBrowserFrame(650)
    await advanceBrowserFrame(680)
    const activeScene = wrapper.find('svg').html()
    expect(frameCallbacks.size).toBe(1)

    // 缓存离页后，浏览器事件也不能重新启动本地动画。
    visible.value = false
    await nextTick()
    expect(frameCallbacks.size).toBe(0)
    document.dispatchEvent(new Event('visibilitychange'))
    for (const listener of motionListeners) listener()
    expect(frameCallbacks.size).toBe(0)

    // 返回后首帧保持原相位，随后仅推进当前页面的有效时间。
    visible.value = true
    await nextTick()
    expect(frameCallbacks.size).toBe(1)
    await advanceBrowserFrame(10000)
    expect(wrapper.find('svg').html()).toBe(activeScene)
    await advanceBrowserFrame(10030)
    expect(wrapper.find('svg').html()).not.toBe(activeScene)
  })

  it('反复离开缓存页面不会重置展开进度或产生重复帧', async () => {
    const visible = ref(true)
    const wrapper = mount(
      defineComponent({
        setup: () => () =>
          h(KeepAlive, null, {
            default: () =>
              visible.value
                ? h(BeltAnimation, {
                    extended: true,
                    capturing: false,
                    frequencyListening: false,
                  })
                : null,
          }),
      }),
    )
    mountedWrappers.push(wrapper)
    await advanceBrowserFrame(0)
    await advanceBrowserFrame(325)
    expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])

    // 多次切换路由时保留中间位置，每次只安排一个刷新回调。
    for (const timestamp of [10000, 20000]) {
      visible.value = false
      await nextTick()
      expect(frameCallbacks.size).toBe(0)
      visible.value = true
      await nextTick()
      expect(frameCallbacks.size).toBe(1)
      await advanceBrowserFrame(timestamp)
      expect(readRollerPositions(wrapper)).toEqual([257.5, 702.5])
    }

    // 展开剩余时间结束后，卸载缓存组件清理所有动画资源。
    await advanceBrowserFrame(20325)
    expect(readRollerPositions(wrapper)).toEqual([200, 760])
    visible.value = false
    await nextTick()
    wrapper.unmount()
    mountedWrappers = []
    expect(frameCallbacks.size).toBe(0)
    expect(motionListeners.size).toBe(0)
  })

  it('独立采集可在皮带停止时运行，卸载后释放所有监听和帧', async () => {
    const wrapper = mountAnimation({ capturing: true })
    expect(frameCallbacks.size).toBe(1)
    await advanceBrowserFrame(0)
    await advanceBrowserFrame(30)
    expect(readRollerPositions(wrapper)).toEqual([315, 645])
    await wrapper.setProps({ capturing: false })
    expect(frameCallbacks.size).toBe(0)
    await wrapper.setProps({ frequencyListening: true })
    expect(frameCallbacks.size).toBe(1)
    wrapper.unmount()
    mountedWrappers = []
    expect(frameCallbacks.size).toBe(0)
    expect(motionListeners.size).toBe(0)
  })
})
