<script setup lang="ts">
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
defineProps<{ page: number; pages: number; total: number; loading?: boolean }>()
defineEmits<{ change: [page: number] }>()
</script>
<template>
  <nav class="pagination" aria-label="分页">
    <span
      >共 <strong>{{ total }}</strong> 条 · 第 {{ page }} / {{ Math.max(1, pages) }} 页</span
    >
    <div class="inline-actions">
      <button
        class="icon-button"
        aria-label="上一页"
        :disabled="loading || page <= 1"
        @click="$emit('change', page - 1)"
      >
        <ChevronLeft :size="18" /></button
      ><button
        class="icon-button"
        aria-label="下一页"
        :disabled="loading || page >= pages"
        @click="$emit('change', page + 1)"
      >
        <ChevronRight :size="18" />
      </button>
    </div>
  </nav>
</template>
