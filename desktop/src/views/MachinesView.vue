<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, reactive, ref } from 'vue'
import { Plus, Pencil, Trash2, Camera, Radio, Cpu, Check } from 'lucide-vue-next'
import { api, ApiError, errorText } from '../lib/api'
import type { Machine } from '../lib/types'
import { useRequest } from '../composables/useRequest'
import { runtime } from '../composables/useRuntime'
import AppDialog from '../components/AppDialog.vue'
import StatePanel from '../components/StatePanel.vue'
const request = useRequest<{ machines: Machine[] }>()
const { data, loading, error } = request
const editor = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const formError = ref('')
const errorField = ref('')
const notice = ref('')
const deleting = ref<Machine | null>(null)
type MachineDraft = Omit<Machine, 'id' | 'created_at' | 'updated_at'>
const drafts = reactive<Record<string, MachineDraft>>({
  new: {
    machine_name: '',
    camera_serial: '',
    frequency_meter_serial: '',
    enabled: true,
    remark: '',
  },
})
const draft = computed(() => drafts[String(editingId.value ?? 'new')])
const locked = computed(() => Boolean(runtime.value?.running))
/**
 * 刷新机器列表并保留编辑草稿。
 * Args: 无。
 * Returns: Promise<void>；列表或错误写入请求状态。
 */
async function load() {
  await request.run((signal) => api('/machines', { signal }))
}
/**
 * 打开所选机器的现有草稿或新建表单。
 * Args: machine: 已选机器，省略表示新建。
 * Returns: 无；editor 打开并保留该机器的草稿。
 */
function editMachine(machine?: Machine) {
  if (saving.value) return
  editingId.value = machine?.id ?? null
  const key = String(editingId.value ?? 'new')
  if (machine && !drafts[key])
    drafts[key] = {
      machine_name: machine.machine_name,
      camera_serial: machine.camera_serial,
      frequency_meter_serial: machine.frequency_meter_serial,
      enabled: machine.enabled,
      remark: machine.remark,
    }
  formError.value = ''
  errorField.value = ''
  editor.value = true
}
/**
 * 提交当前机器草稿并刷新列表。
 * Args: 无。
 * Returns: Promise<void>；成功关闭表单，失败保留草稿。
 */
async function saveMachine() {
  if (saving.value) return
  const id = editingId.value
  saving.value = true
  formError.value = ''
  errorField.value = ''
  try {
    await api(id === null ? '/machines' : `/machines/${id}`, {
      method: id === null ? 'POST' : 'PUT',
      body: JSON.stringify(draft.value),
    })
    delete drafts[String(id ?? 'new')]
    if (id === null)
      drafts.new = {
        machine_name: '',
        camera_serial: '',
        frequency_meter_serial: '',
        enabled: true,
        remark: '',
      }
    editor.value = false
    notice.value = id === null ? '机器已添加，请在系统配置中检查 DI 通道绑定。' : '机器配置已保存。'
    await load()
  } catch (failure) {
    formError.value = errorText(failure)
    errorField.value = failure instanceof ApiError ? failure.field || '' : ''
  } finally {
    saving.value = false
  }
}
/**
 * 提交已确认机器的删除请求。
 * Args: 无。
 * Returns: Promise<void>；完成后刷新列表并显示结果。
 */
async function deleteMachine() {
  if (!deleting.value || saving.value) return
  const deletedId = deleting.value.id
  saving.value = true
  formError.value = ''
  try {
    await api(`/machines/${deletedId}`, { method: 'DELETE' })
    delete drafts[String(deletedId)]
    if (deleting.value?.id === deletedId) deleting.value = null
    notice.value = '机器已移除，已保存的测量记录仍可查询。'
    await load()
  } catch (failure) {
    formError.value = errorText(failure)
  } finally {
    saving.value = false
  }
}
onDeactivated(() => {
  editor.value = false
  deleting.value = null
})
let activated = false
onMounted(load)
onActivated(() => {
  if (activated) void load()
  activated = true
})
</script>
<template>
  <div class="page-stack">
    <div class="page-intro">
      <p class="page-description">
        管理机器与相机、频率仪的对应关系。设备参数在下一次启动监测时生效。
      </p>
      <button class="button primary" :disabled="locked || saving" @click="editMachine()">
        <Plus :size="17" />添加机器
      </button>
    </div>
    <div v-if="locked" class="notice warning">监测正在运行。请先停止监测，再修改设备配置。</div>
    <div v-if="notice" class="notice success" role="status"><Check :size="17" />{{ notice }}</div>
    <StatePanel
      :loading="loading"
      :error="error"
      :empty="Boolean(data && !data.machines.length)"
      title="开始连接你的设备"
      description="添加机器名称、相机序列号与频率仪序列号。"
      @retry="load"
    />
    <div v-if="data?.machines.length && !loading && !error" class="equipment-grid">
      <article v-for="machine in data.machines" :key="machine.id" class="panel equipment-card">
        <div class="equipment-top">
          <span class="equipment-icon"><Cpu :size="24" /></span
          ><span class="badge" :class="machine.enabled ? 'normal' : 'offline'">{{
            machine.enabled ? '已启用' : '未启用'
          }}</span>
        </div>
        <span class="overline">MACHINE {{ String(machine.id).padStart(2, '0') }}</span>
        <h2>{{ machine.machine_name }}</h2>
        <dl class="equipment-connections">
          <div>
            <dt><Camera :size="16" />相机</dt>
            <dd class="mono">{{ machine.camera_serial }}</dd>
          </div>
          <div>
            <dt><Radio :size="16" />频率仪</dt>
            <dd class="mono">{{ machine.frequency_meter_serial }}</dd>
          </div>
        </dl>
        <p class="equipment-remark">{{ machine.remark || '暂无备注' }}</p>
        <div class="equipment-actions">
          <button class="button ghost" :disabled="locked || saving" @click="editMachine(machine)">
            <Pencil :size="15" />编辑机器</button
          ><button
            class="icon-button danger-text"
            :disabled="locked || saving"
            :aria-label="`移除 ${machine.machine_name}`"
            @click="
              ($event) => {
                deleting = machine
                formError = ''
              }
            "
          >
            <Trash2 :size="16" />
          </button>
        </div>
      </article>
    </div>
    <AppDialog
      :open="editor"
      :title="editingId === null ? '添加机器' : '编辑机器'"
      description="使用真实设备序列号，重复与有效性由后端统一校验。关闭窗口后，本次未保存草稿仍会保留。"
      @update:open="
        ($event) => {
          if (!saving) editor = $event
        }
      "
      ><form v-if="draft" @submit.prevent="saveMachine">
        <fieldset :disabled="saving" class="form-fieldset">
          <div class="form-grid">
            <label class="field-label span-two"
              >机器名称<input
                v-model="draft.machine_name"
                required
                maxlength="200"
                :aria-invalid="errorField === 'machine_name'"
                autocomplete="off" /></label
            ><label class="field-label"
              >相机序列号<input
                v-model="draft.camera_serial"
                required
                :aria-invalid="errorField === 'camera_serial'"
                autocomplete="off"
                class="mono" /></label
            ><label class="field-label"
              >频率仪序列号<input
                v-model="draft.frequency_meter_serial"
                required
                :aria-invalid="errorField === 'frequency_meter_serial'"
                autocomplete="off"
                class="mono" /></label
            ><label class="field-label span-two"
              >备注<textarea v-model="draft.remark" rows="3" /></label
            ><label class="check-label span-two"
              ><input v-model="draft.enabled" type="checkbox" />启用此机器</label
            >
          </div>
          <p v-if="formError" class="inline-error" role="alert">{{ formError }}</p>
          <div class="dialog-actions">
            <button
              class="button secondary"
              type="button"
              :disabled="saving"
              @click="editor = false"
            >
              稍后继续</button
            ><button class="button primary" type="submit" :disabled="saving || locked">
              {{ saving ? '正在保存' : '保存机器' }}
            </button>
          </div>
        </fieldset>
      </form></AppDialog
    >
    <AppDialog
      :open="Boolean(deleting)"
      title="移除机器？"
      :description="`将从设备列表移除 ${deleting?.machine_name || ''}。历史测量和审计记录仍会保留。`"
      @update:open="
        ($event) => {
          if (!$event && !saving) deleting = null
        }
      "
      ><p v-if="formError" class="inline-error" role="alert">{{ formError }}</p>
      <div class="dialog-actions">
        <button class="button secondary" :disabled="saving" @click="deleting = null">
          保留机器</button
        ><button class="button danger" :disabled="saving || locked" @click="deleteMachine">
          {{ saving ? '正在移除' : '确认移除' }}
        </button>
      </div></AppDialog
    >
  </div>
</template>
