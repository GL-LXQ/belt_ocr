<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import {
  Play,
  Pause,
  RotateCw,
  Cpu,
  Check,
  Circle,
  Radio,
  ScanText,
  AlertTriangle,
  X,
} from 'lucide-vue-next'
import BeltAnimation from '../components/BeltAnimation.vue'
import StatePanel from '../components/StatePanel.vue'
import AppDialog from '../components/AppDialog.vue'
import { api } from '../lib/api'
import { formatFrequency, formatOcrResultText, stageLabels } from '../lib/format'
import type { RuntimeMachine, Snapshot } from '../lib/types'
import { useRequest } from '../composables/useRequest'
import { currentSession, animationState } from '../lib/runtimeView'
import {
  runtime,
  connection,
  runtimeError,
  runtimeAction,
  controlMonitoring,
  lastUpdate,
  applySnapshot,
} from '../composables/useRuntime'
const selectedId = ref('')
const stopDialog = ref(false)
const summaryRequest = useRequest<{ recognition_count: number; pending_review_count: number }>()
const summary = summaryRequest.data
const snapshotRequest = useRequest<Snapshot>()
const machines = computed(() => runtime.value?.machines ?? [])
const selected = computed(
  () => machines.value.find((machine) => machine.id === selectedId.value) ?? machines.value[0],
)
const activeSession = computed(() => (selected.value ? sessionFor(selected.value) : undefined))
const enabledCount = computed(() => machines.value.filter((machine) => machine.enabled).length)
const onlineCount = computed(
  () => machines.value.filter((machine) => machine.enabled && machine.status === 'online').length,
)
const faultCount = computed(
  () => machines.value.filter((machine) => machine.enabled && machine.status === 'fault').length,
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
/**
 * 显示当前周期的流程状态。
 * Args:
 *   machine: 当前机器快照。
 * Returns:
 *   '本轮测量进行中' // 当前周期的显示文字
 */
function machineStateLabel(machine: RuntimeMachine) {
  const session = sessionFor(machine)
  return session?.state === 'RUNNING'
    ? '本轮测量进行中'
    : session?.state === 'SAVING_RESULT'
      ? '正在保存本轮结果'
      : machine.waiting_cycle_reset
        ? '等待下一轮信号'
        : '等待现场启动'
}
/**
 * 刷新机器快照和今日统计，保留较新的实时快照。
 * Args:
 *   无外部参数。
 * Returns:
 *   Promise<void> // 机器快照与今日统计已刷新，失败时显示错误
 */
async function refreshOverview() {
  const summaryRefresh = refreshSummary()
  const snapshot = await snapshotRequest.run((signal) => api<Snapshot>('/state', { signal }))
  if (snapshot) applySnapshot(snapshot)
  await summaryRefresh
}
// 连接更换时取消手动快照读取，避免旧服务结果覆盖重连状态。
watch(
  connection,
  (state) => {
    if (state !== 'connected') snapshotRequest.cancel()
  },
  { flush: 'sync' },
)
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
  <section class="realtime-layout" aria-label="实时监测工作区">
    <div
      v-if="
        runtimeError ||
        runtime?.failure ||
        snapshotRequest.error.value ||
        summaryRequest.error.value
      "
      class="notice error"
      role="alert"
    >
      <AlertTriangle :size="18" /><span>{{
        runtimeError ||
        runtime?.failure ||
        snapshotRequest.error.value ||
        summaryRequest.error.value
      }}</span>
    </div>
    <div v-if="connection !== 'connected'" class="notice warning" role="status">
      实时连接中断，当前为最后一次同步状态。重新连接后将自动读取完整快照。
    </div>
    <!-- 按原有的两张卡片分组展示设备和今日统计。 -->
    <div class="metrics-grid" aria-label="监测总览">
      <article class="metric-card" aria-labelledby="device-summary-title">
        <div class="summary-heading">
          <Cpu :size="22" />
          <div>
            <h2 id="device-summary-title">设备总览</h2>
            <small>当前启用设备的运行状态</small>
          </div>
        </div>
        <dl class="summary-metrics">
          <div>
            <dt>总机器</dt>
            <dd class="metric-value">{{ enabledCount }}</dd>
          </div>
          <div>
            <dt>在线机器</dt>
            <dd class="metric-value">{{ onlineCount }}</dd>
          </div>
          <div>
            <dt>故障机器</dt>
            <dd class="metric-value" :class="{ 'danger-text': faultCount > 0 }">
              {{ faultCount }}
            </dd>
          </div>
        </dl>
      </article>
      <article class="metric-card" aria-labelledby="today-summary-title">
        <div class="summary-heading">
          <ScanText :size="22" />
          <div>
            <h2 id="today-summary-title">今日检测</h2>
            <small>今日检测结果与待处理情况</small>
          </div>
        </div>
        <dl class="summary-metrics">
          <div>
            <dt>今日识别</dt>
            <dd class="metric-value">{{ summary?.recognition_count ?? '—' }}</dd>
          </div>
          <div>
            <dt><RouterLink to="/history">待复核</RouterLink></dt>
            <dd class="metric-value">{{ summary?.pending_review_count ?? '—' }}</dd>
          </div>
        </dl>
      </article>
    </div>
    <!-- 操作栏只占左列，详情和机器卡片从同一行开始。 -->
    <div class="machine-workspace">
      <div class="section-heading machine-section-heading">
        <div>
          <h2 id="machine-list-title">我的机器</h2>
          <span>实时查看各检测机器的连接、测量、频率与识别状态</span>
        </div>
        <div class="inline-actions">
          <button
            class="button secondary small"
            :disabled="
              snapshotRequest.loading.value ||
              summaryRequest.loading.value ||
              connection !== 'connected'
            "
            @click="refreshOverview"
          >
            <RotateCw :size="15" />刷新
          </button>
          <button
            class="button secondary small"
            :disabled="!runtime?.running || transition || connection !== 'connected'"
            @click="stopDialog = true"
          >
            <Pause :size="15" />{{ runtime?.running && transition ? '正在停止' : '停止监测' }}
          </button>
          <button
            class="button primary small"
            :disabled="runtime?.running || transition || connection !== 'connected'"
            @click="controlMonitoring('start')"
          >
            <Play :size="15" />{{ !runtime?.running && transition ? '正在启动' : '启动监测' }}
          </button>
        </div>
      </div>
      <span class="live-caption"
        ><span class="status-dot" :class="{ online: connection === 'connected' }"></span
        >{{
          lastUpdate
            ? `更新于 ${lastUpdate.toLocaleTimeString('zh-CN', { hour12: false })}`
            : '等待同步'
        }}</span
      >
      <div class="machine-scroll" role="region" aria-labelledby="machine-list-title" tabindex="0">
        <StatePanel
          v-if="!machines.length"
          empty
          title="尚未配置机器"
          description="添加机器与设备序列号后，即可开始现场监测。"
        />
        <div v-else class="machine-grid">
          <button
            v-for="machine in machines"
            :key="machine.id"
            class="machine-card"
            :class="{ selected: selected?.id === machine.id }"
            @click="selectedId = machine.id"
            :aria-pressed="selected?.id === machine.id"
          >
            <div class="machine-card-heading">
              <h3>{{ machine.machine_name }}</h3>
              <span class="badge" :class="machine.status"
                ><span class="status-dot"></span>{{ statusLabels[machine.status] }}</span
              >
            </div>
            <div class="camera-status">
              <span
                class="status-dot"
                :class="{ online: machine.camera_state === '相机已连接' }"
              ></span
              ><span>{{ machine.camera_state }}</span>
            </div>
            <div class="belt-stage">
              <BeltAnimation
                :machine-id="machine.id"
                :session-id="machine.active_session_id"
                :snapshot-sequence="runtime?.sequence ?? 0"
                :capturing="motionFor(machine).capturing"
                :frequency-listening="motionFor(machine).frequencyListening"
                :extended="motionFor(machine).extended"
                :paused="connection !== 'connected'"
              />
            </div>
            <div class="machine-metrics">
              <div>
                <span>本轮流程</span><strong>{{ machineStateLabel(machine) }}</strong>
              </div>
              <div>
                <span>实时频率</span
                ><strong
                  >{{ formatFrequency(sessionFor(machine)?.final_frequency_hz)
                  }}<small v-if="sessionFor(machine)?.final_frequency_hz != null">
                    Hz</small
                  ></strong
                >
              </div>
            </div>
            <div class="machine-ocr-summary">
              <span>本轮识别</span
              ><code :title="sessionFor(machine)?.recognized_lines[0]">{{
                sessionFor(machine)?.recognized_lines[0] || '—'
              }}</code>
            </div>
          </button>
        </div>
      </div>
      <div class="detail-scroll" role="region" aria-label="机器详情" tabindex="0">
        <aside class="panel session-detail">
          <div class="section-heading compact">
            <div>
              <span class="detail-section-label">机器详情</span>
              <h2>{{ selected?.machine_name || '未选择机器' }}</h2>
            </div>
            <span v-if="selected" class="badge" :class="selected.status">{{
              statusLabels[selected.status]
            }}</span>
          </div>
          <dl class="machine-attributes">
            <div>
              <dt>相机序列号</dt>
              <dd>{{ selected?.camera_serial || '—' }}</dd>
            </div>
            <div>
              <dt>频率仪序列号</dt>
              <dd>{{ selected?.frequency_meter_serial || '—' }}</dd>
            </div>
            <div>
              <dt>本轮流程</dt>
              <dd>{{ selected ? machineStateLabel(selected) : '—' }}</dd>
            </div>
            <div>
              <dt>相机状态</dt>
              <dd>{{ selected?.camera_state || '—' }}</dd>
            </div>
          </dl>
          <div class="detail-frequency">
            <span class="result-label"><Radio :size="15" />实时频率</span
            ><strong class="frequency-value"
              >{{ formatFrequency(activeSession?.final_frequency_hz) }}<small>Hz</small></strong
            >
          </div>
          <section class="detail-ocr" aria-labelledby="ocr-result-title">
            <h3 id="ocr-result-title" class="result-label">OCR 识别结果</h3>
            <pre class="ocr-result" tabindex="0">{{
              formatOcrResultText(activeSession?.recognized_lines ?? [])
            }}</pre>
          </section>
          <section class="detail-progress" aria-labelledby="progress-title">
            <h3 id="progress-title" class="result-label">本轮处理</h3>
            <ol class="stage-list">
              <li
                v-for="(label, stage) in stageLabels"
                :key="stage"
                :class="activeSession?.stages[stage] || 'waiting'"
                :aria-label="`${label}：${progressLabels[activeSession?.stages[stage] || ''] || '等待中'}`"
              >
                <span class="stage-icon"
                  ><Check v-if="activeSession?.stages[stage] === 'success'" :size="13" /><X
                    v-else-if="activeSession?.stages[stage] === 'failed'"
                    :size="13" /><Circle v-else :size="9" /></span
                ><span>{{ label }}</span>
              </li>
            </ol>
          </section>
          <dl class="detail-stats">
            <div>
              <dt>运行时长</dt>
              <dd>—</dd>
            </div>
            <div>
              <dt>今日识别数量</dt>
              <dd>—</dd>
            </div>
            <div>
              <dt>今日待复核数量</dt>
              <dd>—</dd>
            </div>
          </dl>
          <div
            v-if="
              selected &&
              (selected.warning || selected.camera_error || activeSession?.errors.length)
            "
            class="notice warning"
          >
            <AlertTriangle :size="16" /><span>{{
              selected.warning || selected.camera_error || activeSession?.errors.join('；')
            }}</span>
          </div>
          <div v-if="selected" class="session-id">
            <span>本轮 Session</span
            ><span class="mono">{{ activeSession?.session_id || '等待新一轮测量' }}</span
            ><span>{{ selected.inflight_count }} 轮处理中</span>
          </div>
        </aside>
      </div>
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
            () => {
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

<style scoped>
/* 实时工作区把剩余高度交给左右两个滚动区。 */
.realtime-layout {
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
  min-height: 0;
}
.realtime-layout > .notice {
  margin: 0;
  flex: none;
}
.metrics-grid {
  grid-template-columns: minmax(0, 11fr) minmax(0, 9fr);
  gap: 16px;
  flex: none;
}
.metric-card {
  min-width: 0;
  height: 128px;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.summary-heading {
  display: flex;
  align-items: center;
  gap: 10px;
}
.summary-heading > svg {
  flex: none;
  color: var(--accent);
}
.summary-heading h2 {
  font-size: 14px;
  font-weight: 600;
}
.summary-heading small {
  display: block;
  margin-top: 2px;
}
.summary-metrics {
  display: flex;
  margin: 0;
  min-height: 0;
}
.summary-metrics > div {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  text-align: center;
}
.summary-metrics > div + div {
  border-left: 1px solid var(--line);
}
.summary-metrics .metric-value {
  margin: 0;
  font-size: 28px;
  line-height: 1.1;
}
.summary-metrics .danger-text {
  color: #b05653;
}
.summary-metrics dt {
  order: 1;
  margin-top: 3px;
  color: var(--muted);
  font-size: 11px;
}
/* 详情保持固定桌面宽度，顶部和机器卡片对齐。 */
.machine-workspace {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  grid-template-rows: auto minmax(0, 1fr);
  column-gap: 16px;
  row-gap: 12px;
  margin-top: 10px;
  align-items: stretch;
}
.machine-section-heading {
  grid-column: 1;
  grid-row: 1;
  margin: 0;
  flex-wrap: wrap;
  gap: 10px;
}
.machine-section-heading > div > span {
  margin-top: 3px;
}
.machine-section-heading .inline-actions {
  gap: 8px;
  flex-wrap: wrap;
}
.live-caption {
  grid-column: 2;
  grid-row: 1;
  justify-content: flex-end;
  align-self: end;
  padding-bottom: 3px;
  font-size: 10px;
  color: var(--muted);
}
.machine-scroll,
.detail-scroll {
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-width: thin;
  scrollbar-color: #d5dae1 transparent;
}
.machine-scroll {
  grid-column: 1;
  grid-row: 2;
  container-type: inline-size;
}
.detail-scroll {
  grid-column: 2;
  grid-row: 2;
}
.machine-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
  padding: 2px;
}
/* 卡片按名称、相机、动画、流程频率和识别摘要排列。 */
.machine-card {
  padding: 12px 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.machine-card-heading {
  padding: 0;
  gap: 8px;
  align-items: center;
  flex-wrap: nowrap;
}
.machine-card-heading h3 {
  margin: 0;
  overflow-wrap: anywhere;
}
.machine-card-heading .badge {
  flex: none;
}
.camera-status {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 11px;
}
.belt-stage {
  width: 100%;
  min-height: 125px;
  max-height: 175px;
  padding: 0;
}
.machine-metrics {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 16px;
}
.machine-metrics > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.machine-metrics span,
.machine-ocr-summary > span {
  color: var(--muted);
  font-size: 11px;
}
.machine-metrics strong {
  font-size: 12px;
  font-weight: 500;
  overflow-wrap: anywhere;
}
.machine-metrics small {
  font-size: 11px;
}
.machine-ocr-summary {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  padding: 6px 10px;
  border-radius: 8px;
  background: #f7f9fc;
}
.machine-ocr-summary code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  line-height: 20px;
}
/* 详情按设备信息、频率、OCR 和横向五步进度排列。 */
.session-detail {
  position: static;
  padding: 18px 20px;
  min-height: 600px;
}
.session-detail .section-heading {
  align-items: center;
  margin: 0 0 18px;
}
.session-detail .section-heading h2 {
  margin-top: 5px;
}
.detail-section-label {
  font-size: 11px;
  color: var(--muted);
}
.machine-attributes {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 24px;
  row-gap: 14px;
  margin: 0;
}
.machine-attributes dt {
  font-size: 11px;
  color: var(--muted);
}
.machine-attributes dd {
  margin: 5px 0 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}
.detail-frequency {
  margin-top: 18px;
  padding: 12px 16px;
  background: #f7f9fc;
  border-radius: 8px;
}
.frequency-value {
  margin-top: 6px;
}
.detail-ocr,
.detail-progress {
  margin-top: 18px;
}
.result-label {
  font-weight: 500;
}
.ocr-result {
  height: 130px;
  margin: 8px 0 0;
  padding: 10px 14px;
  overflow: auto;
  background: #f7f9fc;
  border-radius: 8px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;
  font-size: 13px;
  line-height: 22px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: text;
}
.session-detail .stage-list {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 0;
  margin-top: 8px;
}
.stage-list li {
  flex-direction: column;
  gap: 8px;
  padding: 2px 0;
  text-align: center;
  font-size: 10px;
}
.stage-list li:not(:last-child)::after {
  display: block;
  left: calc(50% + 13px);
  top: 12px;
  width: calc(100% - 26px);
  height: 1px;
}
.stage-icon {
  width: 22px;
  height: 22px;
  flex: none;
}
.detail-stats {
  display: flex;
  margin: 18px 0 0;
  padding: 10px;
  background: #f7f9fc;
  border-radius: 8px;
}
.detail-stats > div {
  flex: 1;
  min-width: 0;
  padding: 0 6px;
  text-align: center;
}
.detail-stats > div + div {
  border-left: 1px solid var(--line);
}
.detail-stats dt {
  color: var(--muted);
  font-size: 10px;
}
.detail-stats dd {
  margin: 5px 0 0;
  font-size: 13px;
}
.session-id {
  margin: 14px 0 0;
  padding: 0;
  background: none;
  gap: 4px;
}
/* 根据机器列自身的可用宽度选择列数。 */
@container (min-width: 876px) {
  .machine-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@container (max-width: 579px) {
  .machine-grid {
    grid-template-columns: 1fr;
  }
}
/* 窄窗口改为自然页面滚动，避免压缩详情和操作按钮。 */
@media (max-width: 1050px) {
  .machine-workspace {
    flex: none;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto auto;
  }
  .machine-scroll {
    grid-column: 1;
    grid-row: 2;
    overflow: visible;
  }
  .detail-scroll {
    grid-column: 1;
    grid-row: 3;
    overflow: visible;
  }
  .live-caption {
    display: none;
  }
  .session-detail {
    min-height: 0;
  }
}
@media (max-width: 680px) {
  .metrics-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
  .machine-card-heading h3 {
    font-size: 14px;
  }
  .machine-card-heading .badge {
    font-size: 10px;
  }
  .machine-section-heading .inline-actions {
    width: 100%;
  }
}
</style>
