<script setup lang="ts">
/**
 * 对话历史列表。
 *
 * 只负责渲染 + 抛出「选中 / 删除」两个意图；
 * 空状态文案属于本组件（因为「没有历史」是列表自身的状态）。
 */

import type { Conversation } from '@/types'

const props = defineProps<{
  conversations: Conversation[]
  currentId: string | null
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'remove', id: string): void
}>()

/** 列表里的时间只显示到分钟，避免每秒重排 */
function formatTime(timestamp: number): string {
  const date = new Date(timestamp)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function handleRemove(id: string, event: MouseEvent) {
  // 删除按钮嵌在会话按钮外，这里显式阻止冒泡以确保不会顺带切换会话
  event.stopPropagation()
  emit('remove', id)
}
</script>

<template>
  <div class="conv-list">
    <p class="conv-list__label">对话历史</p>

    <!-- 空状态：本阶段没有持久化，刷新后必然是空的 -->
    <p v-if="props.conversations.length === 0" class="conv-list__empty">
      还没有对话记录<br />
      点击上方「新对话」开始
    </p>

    <ul v-else class="conv-list__items">
      <li v-for="item in props.conversations" :key="item.id">
        <div
          class="conv"
          :class="{ 'conv--active': item.id === props.currentId }"
          role="button"
          tabindex="0"
          @click="emit('select', item.id)"
          @keydown.enter="emit('select', item.id)"
        >
          <div class="conv__main">
            <span class="conv__title">{{ item.title }}</span>
            <span class="conv__meta">
              {{ formatTime(item.updatedAt) }} · {{ item.messages.length }} 条
            </span>
          </div>

          <button
            type="button"
            class="conv__remove"
            title="删除该对话"
            aria-label="删除该对话"
            @click="handleRemove(item.id, $event)"
          >
            ×
          </button>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.conv-list {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1 1 auto;
}

.conv-list__label {
  margin: 0 0 0.5rem;
  padding: 0 0.5rem;
  font-size: 0.6875rem;
  letter-spacing: 0.06em;
  color: var(--cf-text-subtle);
}

.conv-list__empty {
  margin: 0.25rem 0 0;
  padding: 0 0.5rem;
  font-size: 0.75rem;
  line-height: 1.8;
  color: var(--cf-text-subtle);
}

.conv-list__items {
  margin: 0;
  padding: 0;
  list-style: none;
  overflow-y: auto;
  min-height: 0;
}

.conv {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.4375rem 0.375rem 0.4375rem 0.5rem;
  margin-bottom: 0.125rem;
  border-radius: 8px;
  cursor: pointer;
}

.conv:hover {
  background: var(--cf-surface);
}

.conv--active {
  background: var(--cf-surface-hover);
}

.conv:focus-visible {
  outline: 2px solid var(--cf-accent);
  outline-offset: -2px;
}

.conv__main {
  min-width: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: 0.0625rem;
}

.conv__title {
  font-size: 0.8125rem;
  /* 标题单行省略，避免长标题把侧边栏撑宽 */
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.conv--active .conv__title {
  font-weight: 600;
}

.conv__meta {
  font-size: 0.6875rem;
  color: var(--cf-text-subtle);
}

.conv__remove {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 1.25rem;
  height: 1.25rem;
  padding: 0;
  font: inherit;
  font-size: 0.875rem;
  line-height: 1;
  color: var(--cf-text-subtle);
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  opacity: 0;
}

.conv:hover .conv__remove,
.conv__remove:focus-visible {
  opacity: 1;
}

.conv__remove:hover {
  color: var(--cf-text);
  background: var(--cf-border);
}
</style>
