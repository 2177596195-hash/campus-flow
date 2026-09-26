<script setup lang="ts">
/**
 * 输入区：textarea + 发送按钮。
 *
 * 键盘语义（本组件的核心职责，其余一概不管）：
 * - Enter         -> 发送
 * - Shift + Enter -> 换行（保留 textarea 原生行为，不拦截）
 * - 输入法组合中（中文候选框）按 Enter -> 不发送，交给输入法上屏
 * - 空消息（含纯空白）-> 不发送，发送按钮 disabled
 *
 * 通过 `v-model` 暴露文本，点击左侧示例问题时父级可直接写回 content。
 */

import { computed, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 是否正在等待助手回复（等待期间禁止再次发送） */
    disabled?: boolean
  }>(),
  { disabled: false },
)

const emit = defineEmits<{
  (e: 'send', text: string): void
}>()

/** 输入框文本（父级用 v-model 同步，便于示例问题回填） */
const content = defineModel<string>({ default: '' })

const textareaRef = ref<HTMLTextAreaElement | null>(null)

/** 去掉首尾空白后是否还有内容 —— 空消息的唯一判定 */
const hasContent = computed(() => content.value.trim().length > 0)
const canSend = computed(() => hasContent.value && !props.disabled)

function send() {
  if (!canSend.value) return

  emit('send', content.value.trim())
  content.value = ''
  // 清空后把高度收回一行（样式由 autosize 增量设置）
  resize()
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter') return
  // Shift + Enter 走浏览器默认换行；输入法组合态下的 Enter 属于「上屏确认」
  if (event.shiftKey || event.isComposing) return

  event.preventDefault()
  send()
}

/** 自适应高度：随内容增长，超过上限后内部滚动（上限在 CSS 的 max-height） */
function resize() {
  const el = textareaRef.value
  if (!el) return

  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

watch(content, () => {
  // 等 DOM 更新后再量高度，避免拿到旧值
  requestAnimationFrame(resize)
})

/** 供父级在「点击示例问题」后把焦点交回输入框 */
function focus() {
  textareaRef.value?.focus()
}

defineExpose({ focus })
</script>

<template>
  <div class="composer">
    <div class="composer__box" :class="{ 'composer__box--disabled': props.disabled }">
      <textarea
        ref="textareaRef"
        v-model="content"
        class="composer__textarea"
        rows="1"
        placeholder="输入校园事务，例如：帮我查一下上学期的成绩"
        :disabled="props.disabled"
        @keydown="handleKeydown"
        @input="resize"
      ></textarea>

      <button
        type="button"
        class="composer__send"
        :disabled="!canSend"
        :title="canSend ? '发送' : '请输入内容后发送'"
        @click="send"
      >
        发送
      </button>
    </div>

    <p class="composer__hint">
      Enter 发送 · Shift + Enter 换行
      <span v-if="props.disabled" class="composer__hint-pending"> · 正在生成回复…</span>
    </p>
  </div>
</template>

<style scoped>
.composer {
  max-width: var(--cf-chat-max-width);
  margin: 0 auto;
  padding: 0.75rem 1.5rem 1rem;
}

.composer__box {
  display: flex;
  align-items: flex-end;
  gap: 0.5rem;
  padding: 0.5rem 0.5rem 0.5rem 0.75rem;
  background: var(--cf-surface);
  border: 1px solid var(--cf-border-strong);
  border-radius: 14px;
}

/* 输入框是主区域的视觉焦点：聚焦时给一层柔和描边 */
.composer__box:focus-within {
  border-color: var(--cf-accent);
  box-shadow: 0 0 0 3px var(--cf-accent-soft);
}

.composer__box--disabled {
  background: var(--cf-surface-hover);
}

.composer__textarea {
  flex: 1 1 auto;
  min-width: 0;
  max-height: 11rem;
  padding: 0.375rem 0;
  font: inherit;
  line-height: 1.7;
  color: inherit;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  overflow-y: auto;
  /* 长串（如 URL）不断行时会出现横向滚动，这里强制换行 */
  overflow-wrap: anywhere;
}

.composer__textarea::placeholder {
  color: var(--cf-text-subtle);
}

.composer__send {
  flex: 0 0 auto;
  padding: 0.4375rem 1rem;
  font: inherit;
  font-weight: 500;
  color: var(--cf-on-accent);
  background: var(--cf-accent);
  border: none;
  border-radius: 10px;
  cursor: pointer;
}

.composer__send:disabled {
  color: var(--cf-text-subtle);
  background: var(--cf-border);
  cursor: not-allowed;
}

.composer__hint {
  margin: 0.5rem 0 0;
  font-size: 0.6875rem;
  color: var(--cf-text-subtle);
}

.composer__hint-pending {
  color: var(--cf-text-muted);
}

@media (max-width: 560px) {
  .composer {
    padding: 0.625rem 1rem 0.875rem;
  }
}
</style>
