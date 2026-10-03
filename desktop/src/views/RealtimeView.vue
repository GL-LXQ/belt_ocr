<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import {
  Play,
  Square,
  ArrowUpRight,
  Check,
  Circle,
  Camera,
  Radio,
  ScanText,
  ChevronRight,
  AlertTriangle,
  Activity,
  X,
} from 'lucide-vue-next'
import BeltAnimation from '../components/BeltAnimation.vue'
import StatePanel from '../components/StatePanel.vue'
import AppDialog from '../components/AppDialog.vue'
import { api } from '../lib/api'
import { formatFrequency, stageLabels } from '../lib/format'
import type { RuntimeMachine } from '../lib/types'
import { useRequest } from '../composables/useRequest'
import { currentSession, animationState } from '../lib/runtimeView'
import {
  runtime,
  connection,
  runtimeError,
  runtimeAction,
  controlMonitoring,
  lastUpdate,
} from '../composables/useRuntime'
const selectedId = ref('')
const stopDialog = ref(false)
const summaryRequest = useRequest<{ recognition_count: number; pending_review_count: number }>()
const summary = summaryRequest.data
const machines = computed(() => runtime.value?.machines ?? [])
const selected = computed(
  () => machines.value.find((machine) => machine.id === selectedId.value) ?? machines.value[0],
)
const activeSession = computed(() => (selected.value ? sessionFor(selected.value) : undefined))
const enabledCount = computed(() => machines.value.filter((machine) => machine.enabled).length)
const onlineCount = computed(
  () => machines.value.filter((machine) => machine.status === 'online').length,
)
const pendingCount = computed(
  () =>
    runtime.value?.sessions.filter(
      (session) => session.state === 'RUNNING' || session.state === 'SAVING_RESULT',
    ).length ?? 0,
)
const transition = computed(
  () =>
    runtimeAction.value ||
    runtime.value?.status === 'starting' ||
    runtime.value?.status === 'stopping',
)
const progressLabels: Record<string, string> = {
  running: '进行中',
  success: '已完成',
  failed: '失败',
}
const statusLabels: Record<string, string> = { online: '在线', offline: '离线', fault: '故障' }
/**
 * 查找机器当前或最近的测量结果。
 * Args: machine: 当前机器快照。
 * Returns: Session 或 undefined，不合并其他周期字段。
 */
function sessionFor(machine: RuntimeMachine) {
  return currentSession(runtime.value, machine)
}
/**
 * 读取当前活动周期对应的本地动画开关。
 * Args: machine: 当前机器快照。
 * Returns: {extended, capturing, frequencyListening}。
 */
function motionFor(machine: RuntimeMachine) {
  return animationState(runtime.value, machine)
}
/**
 * 读取今日统计并取消过期结果。
 * Args: 无。
 * Returns: Promise<void>；更新今日检测和待复核数量。
 */
async function refreshSummary() {
  await summaryRequest.run((signal) => api('/records/summary', { signal }))
}
watch(
  () =>
    runtime.value?.sessions
      .filter((session) => session.state === 'COMMITTED')
      .map((session) => session.session_id)
      .join(','),
  () => {
    void refreshSummary()
  },
)
let activated = false
onActivated(() => {
  if (activated) void refreshSummary()
  activated = true
})
onMounted(refreshSummary)
</script>
<template>
  <section class="realtime-layout">
    <div class="operation-bar">
      <div>
        <span class="status-dot" :class="{ online: runtime?.status === 'running' }"></span
        ><strong>{{
          runtime?.status === 'running'
            ? '正在监测生产状态'
            : runtime?.status === 'starting'
              ? '设备正在初始化'
              : runtime?.status === 'stopping'
                ? '正在完成设备清理'
                : '准备好开始下一轮检测'
        }}</strong>
        <p>机器启停跟随现场 DI 信号，采集与识别自动协同。</p>
      </div>
      <button
        v-if="!runtime?.running"
        class="button primary"
        :disabled="transition || connection !== 'connected'"
        @click="controlMonitoring('start')"
      >
        <Play :size="16" />{{ transition ? '正在启动' : '启动监测' }}</button
      ><button
        v-else
        class="button secondary"
        :disabled="transition || connection !== 'connected'"
        @click="stopDialog = true"
      >
        <Square :size="15" />{{ transition ? '正在停止' : '停止监测' }}
      </button>
    </div>
    <div v-if="runtimeError || runtime?.failure" class="notice error" role="alert">
      <AlertTriangle :size="18" /><span>{{ runtimeError || runtime?.failure }}</span>
    </div>
    <div v-if="connection !== 'connected'" class="notice warning" role="status">
      实时连接中断，当前为最后一次同步状态。重新连接后将自动读取完整快照。
    </div>
    <div class="metrics-grid">
      <article class="metric-card">
        <span class="metric-label">在线设备<Radio :size="16" /></span>
        <div class="metric-value">
          {{ onlineCount }}<span>/ {{ enabledCount }}</span>
        </div>
        <small>已启用设备的实时连接状态</small>
      </article>
      <article class="metric-card">
        <span class="metric-label">今日检测<ScanText :size="17" /></span>
        <div class="metric-value">{{ summary?.recognition_count ?? '—' }}<span>轮</span></div>
        <small>已成功入库的测量记录</small>
      </article>
      <article class="metric-card">
        <span class="metric-label">等待复核<AlertTriangle :size="16" /></span>
        <div class="metric-value">{{ summary?.pending_review_count ?? '—' }}<span>轮</span></div>
        <RouterLink to="/history" class="metric-link"
          >前往历史记录<ArrowUpRight :size="13"
        /></RouterLink>
      </article>
      <article class="metric-card">
        <span class="metric-label">进行中的测量<Activity :size="16" /></span>
        <div class="metric-value">{{ pendingCount }}<span>轮</span></div>
        <small>采集、识别或证据入库中</small>
      </article>
    </div>
    <div class="section-heading">
      <div>
        <h2>机器现场</h2>
        <span>{{ machines.length }} 台设备 · 选择机器查看本轮详情</span>
      </div>
      <span class="live-caption"
        ><span class="status-dot" :class="{ online: connection === 'connected' }"></span
        >{{
          lastUpdate
            ? `更新于 ${lastUpdate.toLocaleTimeString('zh-CN', { hour12: false })}`
            : '等待同步'
        }}</span
      >
    </div>
    <StatePanel
      v-if="!machines.length"
      empty
      title="尚未配置机器"
      description="添加机器与设备序列号后，即可开始现场监测。"
    />
    <div v-else class="machine-workspace">
      <div class="machine-grid">
        <button
          v-for="machine in machines"
          :key="machine.id"
          class="machine-card"
          :class="{ selected: selected?.id === machine.id }"
          @click="selectedId = machine.id"
          :aria-pressed="selected?.id === machine.id"
        >
          <div class="machine-card-heading">
            <div>
              <span class="overline">MACHINE {{ String(machine.id).padStart(2, '0') }}</span>
              <h3>{{ machine.machine_name }}</h3>
            </div>
            <span class="badge" :class="machine.status"
              ><span class="status-dot"></span>{{ statusLabels[machine.status] }}</span
            >
          </div>
          <div class="belt-stage">
            <BeltAnimation
              :capturing="motionFor(machine).capturing"
              :frequency-listening="motionFor(machine).frequencyListening"
              :extended="motionFor(machine).extended"
              :paused="connection !== 'connected'"
            />
          </div>
          <div class="machine-card-status">
            <span
              ><span
                class="status-dot"
                :class="{ online: sessionFor(machine)?.state === 'RUNNING' }"
              ></span
              >{{
                sessionFor(machine)?.state === 'RUNNING'
                  ? '本轮测量进行中'
                  : sessionFor(machine)?.state === 'SAVING_RESULT'
                    ? '正在保存本轮结果'
                    : machine.waiting_cycle_reset
                      ? '等待下一轮信号'
                      : '等待现场启动'
              }}</span
            ><ChevronRight :size="16" />
          </div>
          <div class="machine-card-footer">
            <span class="mono">{{ machine.camera_serial }}</span
            ><span>{{ machine.inflight_count }} 轮处理中</span>
          </div>
        </button>
      </div>
      <aside v-if="selected" class="panel session-detail">
        <div class="section-heading compact">
          <div>
            <span class="overline">CURRENT MEASUREMENT</span>
            <h2>{{ selected.machine_name }}</h2>
          </div>
          <span class="badge" :class="selected.status">{{ statusLabels[selected.status] }}</span>
        </div>
        <div class="session-id">
          <span>本轮 Session</span
          ><span class="mono">{{ activeSession?.session_id || '等待新一轮测量' }}</span>
        </div>
        <ol class="stage-list">
          <li
            v-for="(label, stage) in stageLabels"
            :key="stage"
            :class="activeSession?.stages[stage] || 'waiting'"
          >
            <span class="stage-icon"
              ><Check v-if="activeSession?.stages[stage] === 'success'" :size="13" /><X
                v-else-if="activeSession?.stages[stage] === 'failed'"
                :size="13" /><Circle v-else :size="9" /></span
            ><span>{{ label }}</span
            ><small>{{ progressLabels[activeSession?.stages[stage] || ''] || '等待中' }}</small>
          </li>
        </ol>
        <div class="result-block">
          <span class="result-label"><Radio :size="15" />本轮频率</span
          ><strong class="frequency-value"
            >{{ formatFrequency(activeSession?.final_frequency_hz) }}<small>Hz</small></strong
          >
        </div>
        <div class="result-block">
          <span class="result-label"><ScanText :size="15" />本轮识别文字</span>
          <div v-if="activeSession?.recognized_lines.length" class="ocr-lines">
            <code v-for="(line, index) in activeSession.recognized_lines" :key="index">{{
              line
            }}</code>
          </div>
          <p v-else class="muted">
            {{ activeSession ? '等待本轮识别结果' : '新一轮开始后显示结果' }}
          </p>
        </div>
        <div
          v-if="selected.warning || selected.camera_error || activeSession?.errors.length"
          class="notice warning"
        >
          <AlertTriangle :size="16" /><span>{{
            selected.warning || selected.camera_error || activeSession?.errors.join('；')
          }}</span>
        </div>
        <div class="detail-bottom">
          <Camera :size="14" /><span>{{ selected.camera_serial }}</span
          ><span class="mono">{{ selected.frequency_meter_serial }}</span>
        </div>
      </aside>
    </div>
    <AppDialog
      :open="stopDialog"
      title="停止现场监测？"
      description="系统将先完成周期收尾并释放相机、频率仪与 IO 连接。请等待停止状态确认。"
      @update:open="stopDialog = $event"
      ><div class="dialog-actions">
        <button class="button secondary" @click="stopDialog = false">继续监测</button
        ><button
          class="button danger"
          @click="
            ($event) => {
              stopDialog = false
              controlMonitoring('stop')
            }
          "
        >
          停止监测
        </button>
      </div></AppDialog
    >
  </section>
</template>
