<script setup lang="ts">
import { onActivated, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import { ImageOff, LoaderCircle } from 'lucide-vue-next'
import { imageBlob, errorText } from '../lib/api'
const props = withDefaults(
  defineProps<{ path: string; alt: string; active?: boolean; retryable?: boolean }>(),
  { active: true, retryable: true },
)
const source = ref('')
const error = ref('')
const loading = ref(false)
let controller: AbortController | null = null
let generation = 0
/**
 * 释放当前图片的浏览器对象地址。
 * Args: 无。
 * Returns: 无；当前 source 清空。
 */
function release() {
  if (source.value) URL.revokeObjectURL(source.value)
  source.value = ''
}
/** 每次切图取消旧请求，释放上张原图。Args: 无。Returns: 无。 */
async function loadImage() {
  const current = ++generation
  controller?.abort()
  release()
  error.value = ''
  loading.value = false
  if (!props.path || props.active === false) return
  controller = new AbortController()
  loading.value = true
  try {
    const blob = await imageBlob(props.path, controller.signal)
    if (current === generation) source.value = URL.createObjectURL(blob)
  } catch (failure) {
    if (
      current === generation &&
      !(failure instanceof DOMException && failure.name === 'AbortError')
    )
      error.value = errorText(failure)
  } finally {
    if (current === generation) loading.value = false
  }
}
watch(() => [props.path, props.active], loadImage, { immediate: true })
let activated = false
onActivated(() => {
  if (activated) void loadImage()
  activated = true
})
onDeactivated(() => {
  generation++
  controller?.abort()
  release()
  loading.value = false
})
onBeforeUnmount(() => {
  generation++
  controller?.abort()
  release()
})
</script>
<template>
  <div class="authenticated-image" :aria-busy="loading">
    <img
      v-if="source && !error"
      :src="source"
      :alt="alt"
      decoding="async"
      @error="error = '图片无法解码，文件可能已损坏。'"
    />
    <div v-else-if="error" class="image-placeholder" role="status">
      <ImageOff :size="25" /><span>{{ error }}</span
      ><button v-if="retryable !== false" class="text-button" @click.stop="loadImage">
        重新读取
      </button>
    </div>
    <div v-else class="image-placeholder">
      <LoaderCircle v-if="loading" class="spin" :size="23" /><span>{{
        loading ? '正在读取图片' : '图片尚未加载'
      }}</span>
    </div>
  </div>
</template>
