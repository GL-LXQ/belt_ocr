import { onActivated, onMounted, ref } from 'vue'
import { api, query } from '../lib/api'
import { useRequest } from './useRequest'
import type { RecordFilters, RecordMachine, RecordPage } from '../lib/types'
export const emptyFilters = (): RecordFilters => ({
  review_status: '',
  machine_id: '',
  start_date: '',
  end_date: '',
  text_query: '',
  text_match_mode: 'contains',
  text_length: '',
})
/** 保留筛选草稿，仅在查询或翻页时读取服务器。Args: 每页数量。Returns: 筛选、分页、读取状态。 */
export function useRecords(pageSize = 20) {
  const filters = ref(emptyFilters())
  const applied = ref(emptyFilters())
  const machines = ref<RecordMachine[]>([])
  const page = ref(1)
  const request = useRequest<RecordPage>()
  async function load(target = page.value) {
    page.value = target
    const result = await request.run((signal) =>
      api<RecordPage>(`/records${query({ ...applied.value, page: target, page_size: pageSize })}`, {
        signal,
      }),
    )
    if (result && target > result.total_pages) await load(result.total_pages)
  }
  async function search() {
    applied.value = { ...filters.value }
    await load(1)
  }
  function reset() {
    filters.value = emptyFilters()
    void search()
  }
  let activated = false
  onActivated(() => {
    if (activated) void load()
    activated = true
  })
  onMounted(async () => {
    void load()
    try {
      machines.value = (await api<{ machines: RecordMachine[] }>('/records/machines')).machines
    } catch {
      /* 保留列表读取错误作为主要提示。 */
    }
  })
  return { ...request, filters, applied, machines, page, load, search, reset }
}
