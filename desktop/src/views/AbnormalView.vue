<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, ref } from 'vue'
import { Search, RotateCcw, ArrowUpRight, TriangleAlert, Copy, Check } from 'lucide-vue-next'
import { api, query, errorText } from '../lib/api'
import { formatDate } from '../lib/format'
import type { AbnormalEvent } from '../lib/types'
import { useRequest } from '../composables/useRequest'
import StatePanel from '../components/StatePanel.vue'
import AppDialog from '../components/AppDialog.vue'
import PaginationBar from '../components/PaginationBar.vue'
const empty = () => ({ machine_id: '', session_id: '', start_date: '', end_date: '' })
const filters = ref(empty())
const applied = ref(empty())
const machines = ref<string[]>([])
const page = ref(1)
const request = useRequest<{
  events: AbnormalEvent[]
  page: number
  page_size: number
  total: number
  total_pages: number
}>()
const { data, loading, error } = request
const detail = useRequest<{ event: AbnormalEvent | null }>()
const selected = ref<number | null>(null)
const events = computed(() => data.value?.events ?? [])
const copyMessage = ref('')
const copyError = ref('')
const raw = computed(() => {
  const value = detail.data.value?.event?.payload_json || ''
  try {
    return JSON.stringify(JSON.parse(value), null, 2)
  } catch {
    return value
  }
})
/**
 * 读取异常事件的服务端分页。
 * Args: target: 要读取的页码。
 * Returns: Promise<void>；更新事件、总数和分页状态。
 */
async function load(target = page.value) {
  page.value = target
  const result = await request.run((signal) =>
    api(`/abnormal-events${query({ ...applied.value, page: target, page_size: 20 })}`, { signal }),
  )
  if (result && target > result.total_pages) await load(result.total_pages)
}
/**
 * 将筛选草稿应用到异常事件第一页。
 * Args: 无。
 * Returns: 无；发起第一页读取。
 */
function search() {
  applied.value = { ...filters.value }
  page.value = 1
  void load()
}
/**
 * 清空异常筛选并重新查询。
 * Args: 无。
 * Returns: 无；草稿与已应用条件重置。
 */
function reset() {
  filters.value = empty()
  search()
}
/**
 * 读取用户选中的完整异常事件。
 * Args: id: 异常事件主键。
 * Returns: Promise<void>；仅当前选择更新详情。
 */
async function showEvent(id: number) {
  selected.value = id
  copyMessage.value = ''
  copyError.value = ''
  await detail.run((signal) => api(`/abnormal-events/${id}`, { signal }), true)
}
/**
 * 按用户点击复制原始字段并显示结果。
 * Args: value: 原始文本；label: 提示中的字段名称。
 * Returns: Promise<void>；更新复制成功或失败提示。
 */
async function copyValue(value: string, label: string) {
  copyMessage.value = ''
  copyError.value = ''
  try {
    await navigator.clipboard.writeText(value)
    copyMessage.value = `${label}已复制。`
  } catch (failure) {
    copyError.value = `无法复制：${errorText(failure)}`
  }
}
onDeactivated(() => {
  selected.value = null
  detail.cancel()
})
let activated = false
onMounted(async () => {
  void load()
  try {
    machines.value = (await api<{ machine_ids: string[] }>('/abnormal-events/machines')).machine_ids
  } catch {
    /* 页面错误由列表请求展示。 */
  }
})
onActivated(() => {
  if (activated) void load()
  activated = true
})
</script>
<template>
  <div class="page-stack">
    <p class="page-description">保留真实事件与原始内容，追溯采集、识别和通信过程中的异常。</p>
    <form class="filter-panel" @submit.prevent="search">
      <div class="filter-primary">
        <label class="search-field"
          ><Search :size="17" /><input
            v-model="filters.session_id"
            aria-label="完整 Session ID"
            placeholder="按完整 Session ID 查询" /></label
        ><select v-model="filters.machine_id" aria-label="异常机器">
          <option value="">全部机器</option>
          <option v-for="machine in machines" :key="machine" :value="machine">
            机器 {{ machine }}
          </option></select
        ><label class="inline-date">开始<input v-model="filters.start_date" type="date" /></label
        ><label class="inline-date"
          >结束<input
            v-model="filters.end_date"
            type="date"
            :min="filters.start_date || undefined" /></label
        ><button class="button ghost" type="button" @click="reset">
          <RotateCcw :size="15" />重置</button
        ><button class="button primary" type="submit" :disabled="loading">查询</button>
      </div>
    </form>
    <div class="panel table-panel">
      <div class="table-heading">
        <h2>异常事件</h2>
        <span>{{ data?.total ?? '—' }} 条事件</span>
      </div>
      <StatePanel
        :loading="loading"
        :error="error"
        :empty="Boolean(data && !data.events.length)"
        title="当前条件下没有异常事件"
        description="所有异常均按原始记录展示。"
        @retry="load"
      />
      <div v-if="events.length && !loading && !error" class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>记录时间</th>
              <th>机器 / 周期</th>
              <th>异常原因</th>
              <th>事件摘要</th>
              <th><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in events" :key="event.abnormal_event_id">
              <td>
                {{ formatDate(event.created_at)
                }}<span class="table-subtitle mono">EVENT #{{ event.abnormal_event_id }}</span>
              </td>
              <td>
                <strong>机器 {{ event.machine_id }}</strong
                ><span class="table-subtitle mono">{{ event.session_id || '机器级事件' }}</span>
              </td>
              <td>
                <span class="event-reason"><TriangleAlert :size="15" />{{ event.reason }}</span>
              </td>
              <td class="event-summary">{{ event.payload_summary }}</td>
              <td>
                <button
                  class="icon-button"
                  :aria-label="`查看异常事件 ${event.abnormal_event_id}`"
                  @click="showEvent(event.abnormal_event_id)"
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
      :open="selected !== null"
      title="异常事件详情"
      description="原始事件保留完整内容，不修改或推测缺失信息。"
      wide
      @update:open="
        ($event) => {
          if (!$event) {
            selected = null
            detail.cancel()
          }
        }
      "
      ><StatePanel
        :loading="detail.loading.value"
        :error="detail.error.value"
        @retry="selected !== null && showEvent(selected)"
      /><template v-if="detail.data.value?.event"
        ><dl class="metadata-list">
          <div>
            <dt>异常原因</dt>
            <dd>{{ detail.data.value.event.reason }}</dd>
          </div>
          <div>
            <dt>记录时间</dt>
            <dd>{{ formatDate(detail.data.value.event.created_at) }}</dd>
          </div>
          <div>
            <dt>机器</dt>
            <dd>{{ detail.data.value.event.machine_id }}</dd>
          </div>
          <div>
            <dt>测量周期</dt>
            <dd class="mono">{{ detail.data.value.event.session_id || '机器级事件' }}</dd>
          </div>
        </dl>
        <div class="section-heading">
          <h3 class="subheading">原始事件 JSON</h3>
          <div class="inline-actions">
            <button
              v-if="detail.data.value.event.session_id"
              class="button ghost small"
              @click="copyValue(detail.data.value.event.session_id, 'Session ID')"
            >
              <Copy :size="14" />复制 Session ID</button
            ><button
              class="button ghost small"
              @click="copyValue(detail.data.value.event.payload_json, '原始 JSON')"
            >
              <Copy :size="14" />复制原始 JSON
            </button>
          </div>
        </div>
        <p v-if="copyMessage" class="notice success" role="status">
          <Check :size="15" />{{ copyMessage }}
        </p>
        <p v-if="copyError" class="inline-error" role="alert">{{ copyError }}</p>
        <pre class="json-view" tabindex="0">{{ raw }}</pre>
      </template></AppDialog
    >
  </div>
</template>
