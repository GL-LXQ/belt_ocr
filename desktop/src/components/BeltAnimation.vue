<script setup lang="ts">
import {
  computed,
  onActivated,
  onBeforeUnmount,
  onDeactivated,
  onMounted,
  shallowRef,
  useId,
  watch,
} from 'vue'
import {
  advanceBeltState,
  easeExtension,
  EXTENSION_DURATION_MS,
  renderBeltSvg,
  type BeltVisualState,
} from '../lib/belt'
import { writeRuntimeDiagnostic } from '../lib/runtimeDiagnostics'

const props = withDefaults(
  defineProps<{
    capturing: boolean
    frequencyListening: boolean
    extended: boolean
    paused?: boolean
    machineId?: string
    sessionId?: string | null
    snapshotSequence?: number
  }>(),
  { paused: false, machineId: '', sessionId: null, snapshotSequence: 0 },
)

// 保存每张卡片独立的材质标识和本地动画状态。
const instanceId = `belt-${useId()}`
let visualState: BeltVisualState = {
  extension: 0,
  capturing: props.capturing,
  frequencyListening: props.frequencyListening,
  travel: 0,
  scanPhase: 0,
  frequencyPhase: 0,
}
const svgMarkup = shallowRef(renderBeltSvg(visualState, instanceId))
const statusLabel = computed(() =>
  [
    props.extended ? '皮带运行中' : '皮带已停止',
    props.capturing ? '相机采集中' : '',
    props.frequencyListening ? '频率监听中' : '',
  ]
    .filter(Boolean)
    .join('，'),
)

// 保存可中断的展开过程和浏览器刷新句柄。
let transition: { from: number; to: number; elapsedMs: number } | null = null
let frameHandle: number | null = null
let previousFrameTime: number | null = null
let mounted = false
let active = true
let reducedMotion = false
let motionQuery: MediaQueryList | null = null

// 保存当前周期的动画请求时间和首次执行标记。
let animationRequestedAt: number | null = null
let animationFrameStarted = false
let beltRotationStarted = false

/**
 * 记录当前机器的动画步骤和暂停条件。
 * Args:
 *   trace: 动画请求、首帧或滚筒转动步骤。
 * Returns:
 *   undefined // 当前动画状态已交给运行诊断日志
 */
function logAnimationProgress(trace: string): void {
  writeRuntimeDiagnostic(trace, {
    machine_id: props.machineId,
    session_id: props.sessionId,
    sequence: props.snapshotSequence,
    extended: props.extended,
    capturing: props.capturing,
    frequency_listening: props.frequencyListening,
    paused: props.paused,
    page_active: active,
    page_hidden: document.hidden,
    reduced_motion: reducedMotion,
    extension_duration_ms: EXTENSION_DURATION_MS,
    since_animation_request_ms:
      animationRequestedAt === null ? null : Math.round(performance.now() - animationRequestedAt),
  })
}

/**
 * 更新当前 SVG，并在需要运动时安排下一帧。
 * Args:
 *   无外部参数。
 * Returns:
 *   undefined // 当前场景与刷新计划已更新
 */
function updateScene(): void {
  svgMarkup.value = renderBeltSvg(visualState, instanceId)
  const shouldAnimate =
    mounted &&
    active &&
    !props.paused &&
    !reducedMotion &&
    !document.hidden &&
    (transition !== null || props.extended || props.capturing || props.frequencyListening)

  // 暂停、后台、缓存离页和空闲状态立即释放刷新任务。
  if (!shouldAnimate) {
    if (frameHandle !== null) cancelAnimationFrame(frameHandle)
    frameHandle = null
    previousFrameTime = null
    return
  }
  if (frameHandle === null) frameHandle = requestAnimationFrame(advanceFrame)
}

/**
 * 按浏览器经过的时间推进展开与独立子动画。
 * Args:
 *   timestamp: 浏览器提供的帧时间。
 * Returns:
 *   undefined // 本帧场景已更新
 */
function advanceFrame(timestamp: number): void {
  frameHandle = null

  // 记录当前测量首次执行浏览器动画帧的时刻。
  if (props.extended && !animationFrameStarted) {
    animationFrameStarted = true
    logAnimationProgress('ANIMATION_FIRST_FRAME')
  }

  // 恢复动画时从当前时刻继续，避免后台时间造成相位跳变。
  const elapsedMs = previousFrameTime === null ? 0 : timestamp - previousFrameTime
  previousFrameTime = timestamp
  const wasRunning = transition === null && props.extended

  // 记录展开完成后滚筒首次发生位移的时刻。
  if (wasRunning && elapsedMs > 0 && !beltRotationStarted) {
    beltRotationStarted = true
    logAnimationProgress('BELT_ROTATION_STARTED')
  }

  // 使用原始六百五十毫秒缓动，并允许收缩途中重新启动。
  if (transition !== null) {
    transition.elapsedMs += elapsedMs
    const progress = transition.elapsedMs / EXTENSION_DURATION_MS
    visualState.extension =
      transition.from + (transition.to - transition.from) * easeExtension(progress)
    if (progress >= 1) {
      visualState.extension = transition.to
      transition = null
    }
  }

  // 展开完成后滚筒开始运转，相机和传感器分别推进。
  visualState = advanceBeltState(visualState, elapsedMs, wasRunning)
  updateScene()
}

/**
 * 按系统动态效果偏好更新机械场景。
 * Args:
 *   无外部参数。
 * Returns:
 *   undefined // 减少动态效果时停留在静态目标状态
 */
function applyMotionPreference(): void {
  reducedMotion = motionQuery?.matches ?? false
  if (reducedMotion) {
    transition = null
    visualState.extension = props.extended ? 1 : 0
  }
  updateScene()
}

// 业务状态只控制本机动画；帧和相位不经过网络传输。
watch(
  () => props.extended,
  (extended) => {
    // 重置本轮动画计时，并记录收到启动或停止状态时的暂停条件。
    animationRequestedAt = extended ? performance.now() : null
    animationFrameStarted = false
    beltRotationStarted = false
    logAnimationProgress(extended ? 'ANIMATION_START_REQUESTED' : 'ANIMATION_STOP_REQUESTED')

    const target = extended ? 1 : 0
    transition = reducedMotion ? null : { from: visualState.extension, to: target, elapsedMs: 0 }
    if (reducedMotion) visualState.extension = target
    updateScene()
  },
)
watch(
  () => props.capturing,
  (capturing) => {
    visualState.capturing = capturing
    if (capturing) visualState.scanPhase = 0
    updateScene()
  },
)
watch(
  () => props.frequencyListening,
  (listening) => {
    visualState.frequencyListening = listening
    updateScene()
  },
)
watch(() => props.paused, updateScene)

// 安装系统偏好和后台暂停监听，并启动初始展开。
onMounted(() => {
  mounted = true
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  motionQuery.addEventListener('change', applyMotionPreference)
  document.addEventListener('visibilitychange', updateScene)
  if (props.extended) transition = { from: 0, to: 1, elapsedMs: 0 }
  applyMotionPreference()

  // 挂载时已有活动周期，记录本轮动画的初始启动请求。
  if (props.extended) {
    animationRequestedAt = performance.now()
    logAnimationProgress('ANIMATION_START_REQUESTED')
  }
})

// 缓存页面离开时暂停动画，并保留当前展开位置与运动相位。
onDeactivated(() => {
  active = false
  updateScene()
})

// 返回缓存页面时从当前相位继续，不补算离页期间的时间。
onActivated(() => {
  active = true
  updateScene()
})

// 移除所有本地刷新任务和浏览器监听。
onBeforeUnmount(() => {
  mounted = false
  if (frameHandle !== null) cancelAnimationFrame(frameHandle)
  motionQuery?.removeEventListener('change', applyMotionPreference)
  document.removeEventListener('visibilitychange', updateScene)
})
</script>

<template>
  <div class="belt-animation" role="img" :aria-label="statusLabel" v-html="svgMarkup" />
</template>

<style scoped>
.belt-animation {
  display: grid;
  place-items: center;
  width: 100%;
  height: 155px;
  min-height: 125px;
  max-height: 175px;
  overflow: hidden;
}
.belt-animation :deep(svg) {
  display: block;
  width: 100%;
  height: 100%;
  max-height: 175px;
}
</style>
