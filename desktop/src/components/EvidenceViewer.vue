<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { invoke, isTauri } from '@tauri-apps/api/core'
import {
  ChevronLeft,
  ChevronRight,
  FolderOpen,
  ZoomIn,
  ZoomOut,
  RotateCw,
  Image,
} from 'lucide-vue-next'
import { api, errorText } from '../lib/api'
import type { EvidencePage } from '../lib/types'
import { useRequest } from '../composables/useRequest'
import AuthenticatedImage from './AuthenticatedImage.vue'
import StatePanel from './StatePanel.vue'
const props = withDefaults(defineProps<{ sessionId: string; active?: boolean }>(), { active: true })
const request = useRequest<EvidencePage>()
const { data, loading, error } = request
const page = ref(1)
const selected = ref(0)
const zoom = ref(1)
const folderError = ref('')
const currentImage = computed(() => data.value?.images[selected.value])
const native = isTauri()
const stateMessages: Record<string, string> = {
  no_jpg: '证据目录中没有 JPG / JPEG 图片',
  missing_directory: '证据目录不存在',
  access_denied: '没有读取证据目录的权限',
  read_error: '证据目录读取失败',
}
/**
 * 分页读取当前周期的证据文件名。
 * Args: target: 页码；index: 页内位置，-1 表示最后一张。
 * Returns: Promise<void>；仅当前请求更新列表。
 */
async function load(target = page.value, index = 0) {
  page.value = target
  selected.value = index
  zoom.value = 1
  const result = await request.run(
    (signal) =>
      api<EvidencePage>(
        `/records/${encodeURIComponent(props.sessionId)}/evidence?page=${target}&page_size=24`,
        { signal },
      ),
    true,
  )
  if (result && index < 0) selected.value = Math.max(0, result.images.length - 1)
}
/**
 * 切换图片并在边界读取相邻分页。
 * Args: direction: -1 为上一张，1 为下一张。
 * Returns: Promise<void>；更新页码或当前图片。
 */
async function move(direction: number) {
  if (!data.value || loading.value) return
  const next = selected.value + direction
  if (next < 0 && page.value > 1) await load(page.value - 1, -1)
  else if (next >= data.value.images.length && page.value < data.value.total_pages)
    await load(page.value + 1)
  else if (next >= 0 && next < data.value.images.length) {
    selected.value = next
    zoom.value = 1
  }
}
/**
 * 请求桌面按周期标识打开证据目录。
 * Args: 无。
 * Returns: Promise<void>；失败时显示 folderError。
 */
async function openFolder() {
  folderError.value = ''
  try {
    await invoke('open_evidence_folder', { sessionId: props.sessionId })
  } catch (failure) {
    folderError.value = errorText(failure)
  }
}
watch(
  () => [props.sessionId, props.active],
  () => {
    request.cancel()
    data.value = null
    page.value = 1
    selected.value = 0
    folderError.value = ''
    if (props.active !== false && props.sessionId) void load(1)
  },
  { immediate: true },
)
onBeforeUnmount(request.cancel)
</script>
<template>
  <section class="evidence-viewer" aria-label="证据图片">
    <div class="evidence-toolbar">
      <span
        ><Image :size="15" />{{
          data?.count == null ? '图片数量未知' : `现存 ${data.count} 张`
        }}</span
      >
      <div class="inline-actions">
        <button class="icon-button" aria-label="刷新图片列表" :disabled="loading" @click="load(1)">
          <RotateCw :size="15" /></button
        ><button v-if="native" class="button ghost small" @click="openFolder">
          <FolderOpen :size="15" />打开目录
        </button>
      </div>
    </div>
    <p v-if="!native" class="muted">在桌面应用中可直接打开本地证据目录。</p>
    <p v-if="folderError" class="inline-error" role="alert">{{ folderError }}</p>
    <StatePanel
      :loading="loading"
      :error="error"
      :empty="Boolean(data && !data.images.length)"
      :title="data ? stateMessages[data.state] || '当前没有证据图片' : ''"
      description="测量记录仍然保留，可刷新列表或检查证据目录。"
      @retry="load(1)"
    />
    <template v-if="currentImage && !loading"
      ><div
        class="evidence-canvas"
        tabindex="0"
        aria-label="证据图片；使用左右方向键切换"
        @keydown.left.prevent="move(-1)"
        @keydown.right.prevent="move(1)"
      >
        <div
          class="evidence-zoom"
          :style="{ width: `${zoom * 100}%`, minHeight: `${zoom * 300}px` }"
        >
          <AuthenticatedImage
            :path="currentImage.url"
            :alt="currentImage.filename"
            :active="active"
          />
        </div>
      </div>
      <div class="evidence-controls">
        <div class="inline-actions">
          <button
            class="icon-button"
            aria-label="上一张图片"
            :disabled="page === 1 && selected === 0"
            @click="move(-1)"
          >
            <ChevronLeft :size="19" /></button
          ><span class="mono">{{ (page - 1) * 24 + selected + 1 }} / {{ data?.count }}</span
          ><button
            class="icon-button"
            aria-label="下一张图片"
            :disabled="page === data?.total_pages && selected === (data?.images.length || 0) - 1"
            @click="move(1)"
          >
            <ChevronRight :size="19" />
          </button>
        </div>
        <div class="inline-actions">
          <button
            class="icon-button"
            aria-label="缩小图片"
            :disabled="zoom <= 1"
            @click="zoom = Math.max(1, zoom - 0.25)"
          >
            <ZoomOut :size="17" /></button
          ><span class="mono">{{ Math.round(zoom * 100) }}%</span
          ><button
            class="icon-button"
            aria-label="放大图片"
            :disabled="zoom >= 3"
            @click="zoom = Math.min(3, zoom + 0.25)"
          >
            <ZoomIn :size="17" />
          </button>
        </div>
      </div>
      <p class="evidence-filename mono">{{ currentImage.filename }}</p>
      <div class="evidence-file-list" aria-label="当前页图片">
        <button
          v-for="(image, index) in data?.images"
          :key="image.image_id"
          class="evidence-file"
          :class="{ active: selected === index }"
          @click="
            ($event) => {
              selected = index
              zoom = 1
            }
          "
        >
          <Image :size="14" /><span>{{ image.filename }}</span>
        </button>
      </div></template
    >
  </section>
</template>
