<script setup lang="ts">
import { Search, RotateCcw, SlidersHorizontal } from 'lucide-vue-next'
import { ref } from 'vue'
import type { RecordFilters, RecordMachine } from '../lib/types'
defineProps<{ modelValue: RecordFilters; machines: RecordMachine[]; loading?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: RecordFilters]; search: []; reset: [] }>()
const expanded = ref(false)
</script>
<template>
  <form class="filter-panel" @submit.prevent="emit('search')">
    <div class="filter-primary">
      <label class="search-field"
        ><Search :size="17" /><input
          :value="modelValue.text_query"
          @input="
            emit('update:modelValue', {
              ...modelValue,
              text_query: ($event.target as HTMLInputElement).value,
            })
          "
          placeholder="搜索识别文字"
          aria-label="搜索识别文字"
      /></label>
      <label class="sr-only" for="record-machine">机器</label
      ><select
        id="record-machine"
        :value="modelValue.machine_id"
        @change="
          emit('update:modelValue', {
            ...modelValue,
            machine_id: ($event.target as HTMLSelectElement).value,
          })
        "
      >
        <option value="">全部机器</option>
        <option v-for="machine in machines" :key="machine.machine_id" :value="machine.machine_id">
          {{ machine.machine_name }}
        </option>
      </select>
      <label class="sr-only" for="record-review">复核状态</label
      ><select
        id="record-review"
        :value="modelValue.review_status"
        @change="
          emit('update:modelValue', {
            ...modelValue,
            review_status: ($event.target as HTMLSelectElement).value,
          })
        "
      >
        <option value="">全部状态</option>
        <option value="normal">正常</option>
        <option value="pending">待复核</option>
        <option value="reviewed">已复核</option>
      </select>
      <button
        type="button"
        class="button ghost"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <SlidersHorizontal :size="16" />更多筛选</button
      ><button type="submit" class="button primary" :disabled="loading">查询</button>
    </div>
    <div v-if="expanded" class="filter-expanded">
      <label
        >开始日期<input
          type="date"
          :value="modelValue.start_date"
          @input="
            emit('update:modelValue', {
              ...modelValue,
              start_date: ($event.target as HTMLInputElement).value,
            })
          " /></label
      ><label
        >结束日期<input
          type="date"
          :value="modelValue.end_date"
          :min="modelValue.start_date || undefined"
          @input="
            emit('update:modelValue', {
              ...modelValue,
              end_date: ($event.target as HTMLInputElement).value,
            })
          " /></label
      ><label
        >文字匹配<select
          :value="modelValue.text_match_mode"
          @change="
            emit('update:modelValue', {
              ...modelValue,
              text_match_mode: ($event.target as HTMLSelectElement).value,
            })
          "
        >
          <option value="contains">包含文字</option>
          <option value="exact">整行相等</option>
        </select></label
      ><label
        >完整行长度<select
          :value="modelValue.text_length"
          @change="
            emit('update:modelValue', {
              ...modelValue,
              text_length: ($event.target as HTMLSelectElement).value,
            })
          "
        >
          <option value="">不限长度</option>
          <option v-for="length in [20, 8, 3, 2]" :key="length" :value="length">
            {{ length }} 位
          </option>
        </select></label
      ><button type="button" class="button ghost" @click="emit('reset')">
        <RotateCcw :size="15" />重置
      </button>
    </div>
  </form>
</template>
