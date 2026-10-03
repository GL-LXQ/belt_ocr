<script setup lang="ts">
import { LoaderCircle, Inbox, AlertCircle, RotateCw } from 'lucide-vue-next'
defineProps<{
  loading?: boolean
  error?: string
  empty?: boolean
  title?: string
  description?: string
}>()
defineEmits<{ retry: [] }>()
</script>
<template>
  <div
    v-if="loading || error || empty"
    class="state-panel"
    :role="error ? 'alert' : 'status'"
    :aria-busy="loading"
  >
    <LoaderCircle v-if="loading" class="spin" :size="26" /><AlertCircle
      v-else-if="error"
      :size="26"
    /><Inbox v-else :size="30" />
    <strong>{{ loading ? '正在读取' : error ? '暂时无法读取' : title || '暂无记录' }}</strong>
    <p>
      {{
        error ||
        description ||
        (loading ? '正在从本地服务同步数据…' : '尝试调整筛选条件，或等待新的测量完成。')
      }}
    </p>
    <button v-if="error" class="button secondary" @click="$emit('retry')">
      <RotateCw :size="15" />重新读取
    </button>
  </div>
</template>
