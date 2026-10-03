<script setup lang="ts">
import { computed, onDeactivated, ref } from 'vue'
import { api } from '../lib/api'
import { useRequest } from '../composables/useRequest'
import type { Measurement } from '../lib/types'
import { formatDate } from '../lib/format'
import { useRecords } from '../composables/useRecords'
import RecordFilters from '../components/RecordFilters.vue'
import StatePanel from '../components/StatePanel.vue'
import PaginationBar from '../components/PaginationBar.vue'
import EvidenceCard from '../components/EvidenceCard.vue'
import EvidenceViewer from '../components/EvidenceViewer.vue'
import AppDialog from '../components/AppDialog.vue'
const { filters, machines, page, data, loading, error, load, search, reset } = useRecords(12)
const selectedId = ref('')
const detail = useRequest<{ record: Measurement | null }>()
const selected = computed(() => detail.data.value?.record)
/**
 * 读取最新测量详情后显示证据。
 * Args: record: 用户选中的测量记录。
 * Returns: Promise<void>；仅当前选择更新详情。
 */
async function openRecord(record: Measurement) {
  selectedId.value = record.session_id
  await detail.run(
    (signal) => api(`/records/${encodeURIComponent(record.session_id)}`, { signal }),
    true,
  )
}
/**
 * 关闭证据详情并取消待完成请求。
 * Args: 无。
 * Returns: 无；选中标识和详情清空。
 */
function closeRecord() {
  selectedId.value = ''
  detail.cancel()
  detail.data.value = null
}
onDeactivated(closeRecord)
</script>
<template>
  <div class="page-stack">
    <p class="page-description">
      按测量整理视觉证据。图片在需要时读取，保留每一张的原始比例与完整信息。
    </p>
    <RecordFilters
      v-model="filters"
      :machines="machines"
      :loading="loading"
      @search="search"
      @reset="reset"
    />
    <div class="section-heading">
      <h2>测量证据</h2>
      <span>{{ data?.total ?? '—' }} 组测量</span>
    </div>
    <StatePanel
      :loading="loading"
      :error="error"
      :empty="Boolean(data && !data.records.length)"
      @retry="load()"
    />
    <div v-if="data?.records.length && !loading && !error" class="evidence-grid">
      <EvidenceCard
        v-for="record in data.records"
        :key="record.session_id"
        :record="record"
        @open="openRecord(record)"
      />
    </div>
    <PaginationBar
      v-if="data"
      :page="page"
      :pages="data.total_pages"
      :total="data.total"
      :loading="loading"
      @change="load"
    /><AppDialog
      :open="Boolean(selectedId)"
      title="测量证据"
      :description="
        selected ? `${selected.machine_name} · ${formatDate(selected.finish_time)}` : ''
      "
      wide
      @update:open="
        ($event) => {
          if (!$event) closeRecord()
        }
      "
      ><StatePanel
        :loading="detail.loading.value"
        :error="detail.error.value"
        :empty="Boolean(detail.data.value && !selected)"
        title="测量记录不存在"
        @retry="openRecord({ session_id: selectedId } as Measurement)" /><template v-if="selected"
        ><p class="session-caption mono">{{ selected.session_id }}</p>
        <EvidenceViewer :session-id="selected.session_id" /></template
    ></AppDialog>
  </div>
</template>
