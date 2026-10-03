<script setup lang="ts">
import { computed, onDeactivated, reactive, ref, watch } from 'vue'
import { ArrowUpRight, Check, FileCheck, ScanText, Images, AlertTriangle } from 'lucide-vue-next'
import { TabsRoot, TabsList, TabsTrigger, TabsContent } from 'reka-ui'
import { api, errorText } from '../lib/api'
import {
  effectiveLines,
  formatDate,
  formatFrequency,
  reviewLabels,
  reviewStatus,
} from '../lib/format'
import type { Measurement } from '../lib/types'
import { useRequest } from '../composables/useRequest'
import { useRecords } from '../composables/useRecords'
import RecordFilters from '../components/RecordFilters.vue'
import StatePanel from '../components/StatePanel.vue'
import PaginationBar from '../components/PaginationBar.vue'
import AppDialog from '../components/AppDialog.vue'
import EvidenceViewer from '../components/EvidenceViewer.vue'
const { filters, machines, page, data, loading, error, load, search, reset } = useRecords()
const selectedId = ref('')
const detail = useRequest<{ record: Measurement | null }>()
const record = computed(() => detail.data.value?.record)
const tab = ref('result')
const drafts = reactive<Record<string, { edit: boolean; text: string; initialized: boolean }>>({})
const saving = ref(false)
const saveError = ref('')
const saveMessage = ref('')
const draft = computed(() => drafts[selectedId.value])
onDeactivated(() => {
  selectedId.value = ''
})
const pending = computed(() => record.value && reviewStatus(record.value) === 'pending')
/**
 * 重新读取当前周期的完整测量记录。
 * Args: 无。
 * Returns: Promise<void>；仅当前周期的详情可更新。
 */
async function readDetail() {
  const id = selectedId.value
  if (!id) return
  await detail.run((signal) => api(`/records/${encodeURIComponent(id)}`, { signal }), true)
}
watch(selectedId, (id) => {
  saveError.value = ''
  saveMessage.value = ''
  tab.value = 'result'
  detail.cancel()
  detail.data.value = null
  if (id) {
    drafts[id] ??= { edit: false, text: '', initialized: false }
    void readDetail()
  }
})
watch(record, (value) => {
  if (value && drafts[value.session_id] && !drafts[value.session_id].initialized) {
    drafts[value.session_id].text = effectiveLines(value).join('\n')
    drafts[value.session_id].initialized = true
  }
})
/** 服务端执行文字规范化与一次性复核校验。Args: 无。Returns: 无。 */
async function completeReview() {
  if (!record.value || !draft.value || saving.value) return
  const id = record.value.session_id
  const editedText = draft.value.edit ? draft.value.text : null
  saving.value = true
  saveError.value = ''
  saveMessage.value = ''
  try {
    await api(`/records/${encodeURIComponent(id)}/review`, {
      method: 'POST',
      body: JSON.stringify({ edited_text: editedText }),
    })
    if (selectedId.value === id) {
      saveMessage.value = '复核结果已保存。'
      await readDetail()
    }
    await load()
  } catch (failure) {
    if (selectedId.value === id) saveError.value = errorText(failure)
  } finally {
    saving.value = false
  }
}
</script>
<template>
  <div class="page-stack">
    <p class="page-description">查找每轮测量的正式结果、频率与证据，完成需要人工确认的记录。</p>
    <RecordFilters
      v-model="filters"
      :machines="machines"
      :loading="loading"
      @search="search"
      @reset="reset"
    />
    <div class="panel table-panel">
      <div class="table-heading">
        <h2>测量记录</h2>
        <span>{{ data?.total ?? '—' }} 条记录</span>
      </div>
      <StatePanel
        :loading="loading"
        :error="error"
        :empty="Boolean(data && !data.records.length)"
        @retry="load()"
      />
      <div v-if="data?.records.length && !loading && !error" class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>完成时间 / 周期</th>
              <th>机器</th>
              <th>识别文字</th>
              <th class="numeric">频率 / Hz</th>
              <th>复核状态</th>
              <th><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in data.records" :key="item.session_id">
              <td>
                <time>{{ formatDate(item.finish_time) }}</time
                ><span class="table-subtitle mono">{{ item.session_id }}</span>
              </td>
              <td>
                <strong>{{ item.machine_name }}</strong
                ><span class="table-subtitle">机器 {{ item.machine_id }}</span>
              </td>
              <td class="record-lines">
                <code v-for="(line, index) in effectiveLines(item).slice(0, 2)" :key="index">{{
                  line
                }}</code
                ><span v-if="!effectiveLines(item).length" class="muted">暂无文字</span
                ><small v-if="effectiveLines(item).length > 2"
                  >另有 {{ effectiveLines(item).length - 2 }} 行</small
                >
              </td>
              <td class="numeric mono">{{ formatFrequency(item.final_frequency_hz) }}</td>
              <td>
                <span class="badge" :class="reviewStatus(item)">{{
                  reviewLabels[reviewStatus(item)]
                }}</span>
              </td>
              <td>
                <button
                  class="icon-button"
                  :aria-label="`查看测量 ${item.session_id}`"
                  @click="selectedId = item.session_id"
                >
                  <ArrowUpRight :size="18" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <PaginationBar
        v-if="data"
        :page="page"
        :pages="data.total_pages"
        :total="data.total"
        :loading="loading"
        @change="load"
      />
    </div>
    <AppDialog
      :open="Boolean(selectedId)"
      title="测量详情"
      :description="
        record
          ? `${record.machine_name} · ${formatDate(record.finish_time)}`
          : '读取完整测量信息与证据'
      "
      wide
      @update:open="
        ($event) => {
          if (!$event) selectedId = ''
        }
      "
      ><StatePanel
        :loading="detail.loading.value"
        :error="detail.error.value"
        :empty="Boolean(detail.data.value && !record)"
        title="记录已不存在"
        @retry="readDetail" /><template v-if="record"
        ><div class="detail-summary">
          <span class="session-caption mono">{{ record.session_id }}</span
          ><span class="badge" :class="reviewStatus(record)">{{
            reviewLabels[reviewStatus(record)]
          }}</span>
        </div>
        <TabsRoot v-model="tab"
          ><TabsList class="tabs" aria-label="测量详情内容"
            ><TabsTrigger value="result"><ScanText :size="16" />识别与复核</TabsTrigger
            ><TabsTrigger value="evidence"><Images :size="16" />图片证据</TabsTrigger></TabsList
          ><TabsContent value="result"
            ><div class="detail-stat-grid">
              <div>
                <span>最终频率</span
                ><strong>{{ formatFrequency(record.final_frequency_hz) }}<small> Hz</small></strong>
              </div>
              <div>
                <span>开始时间</span>
                <p>{{ formatDate(record.start_time) }}</p>
              </div>
              <div>
                <span>完成时间</span>
                <p>{{ formatDate(record.finish_time) }}</p>
              </div>
            </div>
            <section class="result-block">
              <h3>正式 OCR 结果</h3>
              <div class="ocr-lines">
                <code v-for="(line, index) in record.recognized_lines" :key="index">{{
                  line
                }}</code>
                <p v-if="!record.recognized_lines.length" class="muted">未保存正式识别文字</p>
              </div>
            </section>
            <section v-if="record.reviewed_at" class="result-block">
              <h3>人工复核结果</h3>
              <div v-if="record.reviewed_lines !== null" class="ocr-lines">
                <code v-for="(line, index) in record.reviewed_lines" :key="index">{{ line }}</code>
              </div>
              <p v-else>已确认原始 OCR 结果</p>
              <p class="muted">复核时间 {{ formatDate(record.reviewed_at) }}</p>
            </section>
            <div v-if="pending && draft" class="review-panel">
              <div class="notice warning">
                <AlertTriangle :size="17" /><span>{{
                  record.review_reason || '本轮结果需要人工确认。请结合证据图片检查。'
                }}</span>
              </div>
              <label class="check-label"
                ><input
                  v-model="draft.edit"
                  type="checkbox"
                  :disabled="saving"
                />手动修正识别文字</label
              ><label v-if="draft.edit" class="field-label"
                >复核文字（每行一条）<textarea
                  v-model="draft.text"
                  :disabled="saving"
                  rows="5"
                  spellcheck="false"
                  class="mono"
                  placeholder="输入已核实的文字"
                /><small>保存时由后端去除空白并转为大写。</small></label
              >
              <p v-else class="muted">将确认当前正式 OCR 结果，保留原始文字。</p>
              <p v-if="saveError" class="inline-error" role="alert">{{ saveError }}</p>
              <button class="button primary" :disabled="saving" @click="completeReview">
                <FileCheck :size="16" />{{
                  saving ? '正在保存' : draft.edit ? '保存修正并完成复核' : '确认原结果并完成复核'
                }}
              </button>
            </div>
            <p v-if="saveMessage" class="notice success" role="status">
              <Check :size="16" />{{ saveMessage }}
            </p></TabsContent
          ><TabsContent value="evidence"
            ><EvidenceViewer
              :session-id="record.session_id"
              :active="tab === 'evidence'" /></TabsContent></TabsRoot></template
    ></AppDialog>
  </div>
</template>
