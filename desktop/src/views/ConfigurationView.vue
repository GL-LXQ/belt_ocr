<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue'
import {
  Camera,
  Cable,
  Radio,
  ScanText,
  Timer,
  Save,
  Check,
  RotateCcw,
  ShieldCheck,
  LockKeyhole,
  ChevronDown,
} from 'lucide-vue-next'
import { api, ApiError, errorText } from '../lib/api'
import type { Configuration, Machine } from '../lib/types'
import { runtime } from '../composables/useRuntime'
import AppDialog from '../components/AppDialog.vue'
import StatePanel from '../components/StatePanel.vue'
interface ConfigField {
  key: string
  label: string
  unit?: string
  nullable?: boolean
  type: 'number' | 'text' | 'boolean'
  hint?: string
}
const groups: { name: string; icon: typeof Camera; description: string; fields: ConfigField[] }[] =
  [
    {
      name: '相机采集',
      icon: Camera,
      description: '相机打开时写入的公共参数。留空的可选参数沿用设备设置。',
      fields: [
        {
          key: 'camera_exposure_time_us',
          label: '曝光时间',
          unit: 'μs',
          nullable: true,
          type: 'number',
        },
        { key: 'camera_gain', label: '增益', nullable: true, type: 'number' },
        {
          key: 'camera_line_selector',
          label: '线路选择器',
          nullable: true,
          type: 'text',
          hint: '例如 Line2，以设备实际可用值为准',
        },
        { key: 'camera_line_mode', label: '线路模式', nullable: true, type: 'text' },
        { key: 'camera_line_source', label: '线路信号源', nullable: true, type: 'text' },
        { key: 'camera_strobe_enabled', label: '频闪输出', nullable: true, type: 'boolean' },
      ],
    },
    {
      name: 'IO 通信',
      icon: Cable,
      description: 'Modbus RTU 串口连接与设备地址。',
      fields: [
        { key: 'modbus_serial_port', label: '串口', type: 'text' },
        { key: 'modbus_baudrate', label: '波特率', type: 'number', unit: 'bps' },
        { key: 'modbus_unit_id', label: '设备地址', type: 'number' },
      ],
    },
    {
      name: '频率采集',
      icon: Radio,
      description: '有效频率范围由后端校验和执行。',
      fields: [
        { key: 'minimum_frequency_hz', label: '有效频率下限', type: 'number', unit: 'Hz' },
        { key: 'maximum_frequency_hz', label: '有效频率上限', type: 'number', unit: 'Hz' },
      ],
    },
    {
      name: '识别与周期',
      icon: ScanText,
      description: '共享识别资源等待、处理和测量周期的时间限制。',
      fields: [
        { key: 'ocr_lock_wait_timeout_ms', label: 'OCR 锁等待期限', type: 'number', unit: 'ms' },
        { key: 'ocr_result_timeout_ms', label: 'OCR 处理期限', type: 'number', unit: 'ms' },
        { key: 'max_cycle_open_ms', label: '测量周期期限', type: 'number', unit: 'ms' },
      ],
    },
  ]
const editable = new Set([
  ...groups.flatMap((group) => group.fields.map((field) => field.key)),
  'io_machine_channels',
])
const settings = ref<Configuration>({})
const draft = ref<Configuration>({})
const revision = ref('')
const baseline = ref('')
const machines = ref<Machine[]>([])
const invalidFields = ref<{ field: string; raw_value: string; message: string }[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const fieldError = ref('')
const message = ref('')
const conflict = ref(false)
const reloadDialog = ref(false)
const initialized = ref(false)
let loadGeneration = 0
let loadController: AbortController | null = null
const dirty = computed(() => JSON.stringify(draft.value) !== baseline.value)
const locked = computed(() => Boolean(runtime.value?.running))
const readonlyFields = computed(() =>
  Object.entries(settings.value).filter(([key]) => !editable.has(key)),
)
const channels = computed(
  () => (draft.value.io_machine_channels as Record<string, number | string | null>) || {},
)
const channelIds = computed(() => [
  ...new Set([
    ...Object.keys(channels.value),
    ...machines.value.map((machine) => String(machine.id)),
  ]),
])
/** 只将允许编辑的字段保存在草稿中。Args: 无。Returns: 无。 */
async function load() {
  loadController?.abort()
  loadController = new AbortController()
  const current = ++loadGeneration
  const signal = loadController.signal
  loading.value = true
  error.value = ''
  fieldError.value = ''
  message.value = ''
  try {
    const [configuration, list] = await Promise.all([
      api<{
        settings: Configuration
        revision: string
        invalid_fields?: { field: string; raw_value: string; message: string }[]
      }>('/configuration', { signal }),
      api<{ machines: Machine[] }>('/machines', { signal }),
    ])
    if (current !== loadGeneration) return
    invalidFields.value = configuration.invalid_fields || []
    settings.value = configuration.settings
    revision.value = configuration.revision
    machines.value = list.machines
    draft.value = structuredClone(
      Object.fromEntries(
        Object.entries(configuration.settings).filter(([key]) => editable.has(key)),
      ),
    )
    baseline.value = JSON.stringify(draft.value)
    conflict.value = false
    initialized.value = true
  } catch (failure) {
    if (current === loadGeneration) error.value = errorText(failure)
  } finally {
    if (current === loadGeneration) loading.value = false
  }
}
/**
 * 保存输入原值，防止非有限数值被 JSON 转为空值。
 * Args: field: 当前字段；value: 输入文字。
 * Returns: 无；只更新对应草稿字段。
 */
function updateField(field: ConfigField, value: string) {
  const numeric = Number(value)
  draft.value[field.key] =
    field.nullable && value === ''
      ? null
      : field.type === 'number'
        ? value === ''
          ? null
          : Number.isFinite(numeric)
            ? numeric
            : value
        : value
  invalidFields.value = invalidFields.value.filter(
    (item) => item.field !== field.key || !Number.isFinite(numeric),
  )
  message.value = ''
}
/**
 * 更新 DI 草稿并保留清空或异常输入给后端校验。
 * Args: id: 机器编号；value: 输入文字。
 * Returns: 无；草稿通道更新，未保存文件。
 */
function updateChannel(id: string, value: string) {
  const updated = { ...channels.value }
  const numeric = Number(value)
  updated[id] = value === '' ? null : Number.isFinite(numeric) ? numeric : value
  invalidFields.value = invalidFields.value.filter(
    (item) => item.field !== `io_machine_channels.${id}` || !Number.isFinite(numeric),
  )
  draft.value.io_machine_channels = updated
  message.value = ''
}
/**
 * 向后端校验或带版本保存可编辑配置。
 * Args: save: true 保存，false 仅校验。
 * Returns: Promise<void>；显示成功、字段错误或版本冲突。
 */
async function submit(save: boolean) {
  if (saving.value) return
  saving.value = true
  error.value = ''
  fieldError.value = ''
  message.value = ''
  try {
    const result = await api<{ settings: Configuration; revision?: string }>(
      save ? '/configuration' : '/configuration/validate',
      {
        method: save ? 'PUT' : 'POST',
        body: JSON.stringify({
          settings: draft.value,
          ...(save ? { revision: revision.value } : {}),
        }),
      },
    )
    if (save) {
      settings.value = result.settings
      revision.value = result.revision || revision.value
      draft.value = structuredClone(
        Object.fromEntries(Object.entries(result.settings).filter(([key]) => editable.has(key))),
      )
      baseline.value = JSON.stringify(draft.value)
      message.value = '配置已保存，下次启动监测时生效。'
    } else message.value = '配置校验通过，尚未写入文件。'
  } catch (failure) {
    error.value = errorText(failure)
    fieldError.value = failure instanceof ApiError ? failure.field || '' : ''
    conflict.value = failure instanceof ApiError && failure.status === 409
  } finally {
    saving.value = false
  }
}
/**
 * 检查草稿后请求重新读取配置。
 * Args: 无。
 * Returns: 无；存在修改时先显示确认对话框。
 */
function requestReload() {
  if (dirty.value) reloadDialog.value = true
  else void load()
}
/**
 * 中止过期配置读取并关闭重新读取确认。
 * Args: 无。
 * Returns: 无；已编辑草稿保持不变。
 */
function cancelLoad() {
  loadGeneration++
  loadController?.abort()
  loading.value = false
  reloadDialog.value = false
}
onDeactivated(cancelLoad)
onBeforeUnmount(cancelLoad)
let activated = false
onMounted(load)
onActivated(() => {
  if (activated && (!initialized.value || !dirty.value) && !saving.value) void load()
  activated = true
})
</script>
<template>
  <div class="page-stack configuration-page">
    <div class="page-intro">
      <p class="page-description">统一管理现场采集参数。修改先保留为草稿，校验通过后再保存。</p>
      <span v-if="dirty && initialized" class="badge pending">有未保存修改</span
      ><span v-else-if="initialized" class="badge normal"><Check :size="13" />已与配置同步</span>
    </div>
    <div v-if="locked" class="notice warning">
      监测运行期间不能保存配置。请先停止监测，已有草稿会保留。
    </div>
    <StatePanel
      v-if="loading || !initialized"
      :loading="loading"
      :error="error"
      @retry="load"
    /><template v-if="initialized && !loading"
      ><div v-if="invalidFields.length" class="notice warning" role="alert">
        <span
          >配置包含需要修正的非有限数值：<span v-for="item in invalidFields" :key="item.field"
            >{{ item.field }} = {{ item.raw_value }}（{{ item.message }}）。</span
          ></span
        >
      </div>
      <form @submit.prevent="submit(true)">
        <fieldset :disabled="saving" class="form-fieldset">
          <div class="configuration-sections">
            <section v-for="group in groups" :key="group.name" class="panel configuration-section">
              <div class="configuration-heading">
                <span class="config-icon"><component :is="group.icon" :size="20" /></span>
                <div>
                  <h2>{{ group.name }}</h2>
                  <p>{{ group.description }}</p>
                </div>
              </div>
              <div class="form-grid">
                <label
                  v-for="field in group.fields"
                  :key="field.key"
                  class="field-label"
                  :for="field.key"
                  >{{ field.label
                  }}<span class="input-unit"
                    ><select
                      v-if="field.type === 'boolean'"
                      :id="field.key"
                      :value="draft[field.key] === null ? '' : String(draft[field.key])"
                      :aria-invalid="
                        fieldError === field.key ||
                        invalidFields.some((item) => item.field === field.key)
                      "
                      @change="
                        draft[field.key] =
                          ($event.target as HTMLSelectElement).value === ''
                            ? null
                            : ($event.target as HTMLSelectElement).value === 'true'
                      "
                    >
                      <option value="">沿用设备设置</option>
                      <option value="true">开启</option>
                      <option value="false">关闭</option></select
                    ><input
                      v-else
                      :id="field.key"
                      :type="
                        invalidFields.some((item) => item.field === field.key) ? 'text' : field.type
                      "
                      :value="draft[field.key] ?? ''"
                      :step="field.type === 'number' ? 'any' : undefined"
                      :placeholder="field.nullable ? '沿用设备设置' : ''"
                      :aria-invalid="
                        fieldError === field.key ||
                        invalidFields.some((item) => item.field === field.key)
                      "
                      @input="updateField(field, ($event.target as HTMLInputElement).value)"
                    /><span v-if="field.unit">{{ field.unit }}</span></span
                  ><small v-if="field.hint">{{ field.hint }}</small></label
                >
              </div>
            </section>
            <section class="panel configuration-section">
              <div class="configuration-heading">
                <span class="config-icon"><Cable :size="20" /></span>
                <div>
                  <h2>机器与 DI 通道</h2>
                  <p>绑定实际机器编号与数字输入通道，由服务端检查冲突和缺失。</p>
                </div>
              </div>
              <div v-if="!channelIds.length" class="muted">添加机器后，可在此配置 DI 通道。</div>
              <div v-else class="channel-list">
                <label v-for="id in channelIds" :key="id" class="channel-row"
                  ><span
                    >{{
                      machines.find((machine) => String(machine.id) === id)?.machine_name ||
                      `保留绑定 · 机器 ${id}`
                    }}<small>机器 {{ id }}</small></span
                  ><span class="input-unit"
                    ><input
                      :type="
                        invalidFields.some((item) => item.field === `io_machine_channels.${id}`)
                          ? 'text'
                          : 'number'
                      "
                      step="1"
                      :value="channels[id] ?? ''"
                      :aria-label="`机器 ${id} DI 通道`"
                      :aria-invalid="fieldError === `io_machine_channels.${id}`"
                      placeholder="未绑定"
                      @input="updateChannel(id, ($event.target as HTMLInputElement).value)"
                    /><span>DI</span></span
                  ></label
                >
              </div>
            </section>
            <details class="panel readonly-section">
              <summary>
                <span><LockKeyhole :size="17" />只读运行参数</span><ChevronDown :size="17" />
              </summary>
              <p class="muted">以下配置由部署环境管理，本页不会写入这些字段。</p>
              <dl class="readonly-list">
                <div v-for="[key, value] in readonlyFields" :key="key">
                  <dt class="mono">{{ key }}</dt>
                  <dd class="mono">
                    {{
                      value === null
                        ? '未设置'
                        : typeof value === 'object'
                          ? JSON.stringify(value)
                          : String(value)
                    }}
                  </dd>
                </div>
              </dl>
            </details>
          </div>
          <div class="configuration-footer">
            <div class="configuration-messages">
              <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
              <p v-if="conflict" class="muted">
                草稿仍保留。请重新读取最新配置，再检查并应用需要的修改。
              </p>
              <p v-if="message" class="save-message" role="status">
                <Check :size="16" />{{ message }}
              </p>
              <span v-if="!error && !message" class="muted">{{
                dirty ? '修改尚未写入配置文件' : '配置为当前已保存版本'
              }}</span>
            </div>
            <div class="inline-actions">
              <button class="button ghost" type="button" :disabled="saving" @click="requestReload">
                <RotateCcw :size="15" />重新读取</button
              ><button
                class="button secondary"
                type="button"
                :disabled="saving"
                @click="submit(false)"
              >
                <ShieldCheck :size="16" />校验配置</button
              ><button
                class="button primary"
                type="submit"
                :disabled="saving || locked || conflict || !dirty"
              >
                <Save :size="16" />{{ saving ? '正在处理' : '保存配置' }}
              </button>
            </div>
          </div>
        </fieldset>
      </form></template
    ><AppDialog
      :open="reloadDialog"
      title="重新读取并放弃草稿？"
      description="当前未保存的修改将被最新已保存配置替换。"
      @update:open="reloadDialog = $event"
      ><div class="dialog-actions">
        <button class="button secondary" @click="reloadDialog = false">保留草稿</button
        ><button
          class="button danger"
          @click="
            ($event) => {
              reloadDialog = false
              load()
            }
          "
        >
          放弃草稿并读取
        </button>
      </div></AppDialog
    >
  </div>
</template>
