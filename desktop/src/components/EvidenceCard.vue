<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue'
import { Images, ArrowUpRight } from 'lucide-vue-next'
import type { EvidencePage, Measurement } from '../lib/types'
import { api } from '../lib/api'
import { useRequest } from '../composables/useRequest'
import {
  effectiveLines,
  formatDate,
  formatFrequency,
  reviewLabels,
  reviewStatus,
} from '../lib/format'
import AuthenticatedImage from './AuthenticatedImage.vue'
const props = defineProps<{ record: Measurement }>()
defineEmits<{ open: [] }>()
const root = ref<HTMLElement | null>(null)
const visible = ref(false)
const request = useRequest<EvidencePage>()
const { data, error } = request
let observer: IntersectionObserver | undefined
const image = computed(() => data.value?.images[0])
/**
 * 进入视野后读取当前测量的一张预览信息。
 * Args: 无。
 * Returns: 无；服务器结果写入当前卡片。
 */
function readEvidence() {
  visible.value = true
  void request.run((signal) =>
    api<EvidencePage>(
      `/records/${encodeURIComponent(props.record.session_id)}/evidence?page=1&page_size=1`,
      { signal },
    ),
  )
}
onMounted(() => {
  if (!('IntersectionObserver' in window)) return readEvidence()
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer?.disconnect()
        readEvidence()
      }
    },
    { rootMargin: '100px' },
  )
  if (root.value) observer.observe(root.value)
})
onDeactivated(() => {
  observer?.disconnect()
  request.cancel()
})
onActivated(() => {
  if (visible.value && !data.value) readEvidence()
  else if (!visible.value && root.value) observer?.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>
<template>
  <button
    ref="root"
    class="evidence-card"
    :aria-label="`查看 ${record.machine_name} 的测量证据 ${record.session_id}`"
    @click="$emit('open')"
  >
    <div class="evidence-card-preview">
      <AuthenticatedImage
        v-if="image"
        :path="image.thumbnail_url"
        :alt="image.filename"
        :active="visible"
        :retryable="false"
      />
      <div v-else class="image-placeholder">
        <Images :size="28" /><span>{{
          error
            ? '图片列表读取失败'
            : !visible
              ? '进入视野后读取'
              : data?.state === 'missing_directory'
                ? '证据目录不存在'
                : data && !data.images.length
                  ? '暂无证据图片'
                  : '正在读取证据'
        }}</span>
      </div>
      <span class="image-count">{{
        data?.count == null ? '数量未知' : `现存 ${data.count} 张`
      }}</span>
    </div>
    <div class="evidence-card-body">
      <div class="evidence-card-title">
        <h3>{{ record.machine_name }}</h3>
        <span class="badge" :class="reviewStatus(record)">{{
          reviewLabels[reviewStatus(record)]
        }}</span>
      </div>
      <p class="evidence-text mono">{{ effectiveLines(record).join(' / ') || '暂无识别文字' }}</p>
      <div class="evidence-card-meta">
        <span>{{ formatFrequency(record.final_frequency_hz) }} Hz</span><ArrowUpRight :size="15" />
      </div>
      <time>{{ formatDate(record.finish_time) }}</time>
    </div>
  </button>
</template>
