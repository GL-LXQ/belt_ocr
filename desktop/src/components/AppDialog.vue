<script setup lang="ts">
import {
  DialogRoot,
  DialogPortal,
  DialogOverlay,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from 'reka-ui'
import { X } from 'lucide-vue-next'
defineProps<{ open: boolean; title: string; description?: string; wide?: boolean }>()
defineEmits<{ 'update:open': [value: boolean] }>()
</script>
<template>
  <DialogRoot :open="open" @update:open="$emit('update:open', $event)">
    <DialogPortal
      ><DialogOverlay class="dialog-overlay" /><DialogContent
        class="dialog-panel"
        :class="{ 'dialog-wide': wide }"
      >
        <div class="dialog-heading">
          <div>
            <DialogTitle class="dialog-title">{{ title }}</DialogTitle
            ><DialogDescription class="muted" v-if="description">{{
              description
            }}</DialogDescription
            ><DialogDescription v-else class="sr-only">{{ title }}</DialogDescription>
          </div>
          <DialogClose class="icon-button" aria-label="关闭对话框"><X :size="19" /></DialogClose>
        </div>
        <slot /> </DialogContent
    ></DialogPortal>
  </DialogRoot>
</template>
