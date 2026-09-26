<script setup lang="ts">
/**
 * 欢迎页示例问题卡片。
 *
 * 职责单一：一张可点击的卡片。
 * 点击只把 question 冒泡给父级（父级负责填入输入框），**不发任何请求**。
 */

const props = defineProps<{
  title: string
  question: string
}>()

const emit = defineEmits<{
  (e: 'select', question: string): void
}>()

function handleClick() {
  emit('select', props.question)
}
</script>

<template>
  <button
    type="button"
    class="prompt"
    :title="question"
    @click="handleClick"
  >
    <span class="prompt__title">{{ title }}</span>
    <span class="prompt__question">{{ question }}</span>
  </button>
</template>

<style scoped>
.prompt {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  width: 100%;
  padding: 0.75rem 0.875rem;
  font: inherit;
  text-align: left;
  color: inherit;
  background: var(--cf-surface);
  border: 1px solid var(--cf-border);
  border-radius: 10px;
  cursor: pointer;
}

.prompt:hover {
  border-color: var(--cf-border-strong);
  background: var(--cf-surface-hover);
}

.prompt:focus-visible {
  outline: 2px solid var(--cf-accent);
  outline-offset: 1px;
}

.prompt__title {
  font-size: 0.8125rem;
  font-weight: 600;
}

.prompt__question {
  font-size: 0.75rem;
  line-height: 1.6;
  color: var(--cf-text-muted);
  /* 卡片内文字也要能换行，避免长问句把卡片撑宽 */
  overflow-wrap: anywhere;
}
</style>
