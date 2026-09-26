<script setup lang="ts">
/**
 * 聊天主页：侧边栏 + 消息区 + 输入区。
 *
 * F3 起数据流分成两条（保持 F2 的组件拆分与 store 结构不变）：
 *   1. **考试安排**（真实链路）：
 *      发送 → store 写入 user 消息 → 调用 Spring Boot `GET /api/exams`
 *      → 真实返回数据写入 assistant 消息；失败则写入明确的失败提示，绝不编造数据。
 *   2. 其它问题（仍是本地占位）：
 *      发送 → store 写入 user 消息 → 本地占位回复写入 assistant 消息（标注「占位」）。
 *
 * 这里是「页面编排」层：布局、窄屏抽屉、发送流程、两条分支的分流、滚动跟随。
 */

import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppSidebar from '@/components/sidebar/AppSidebar.vue'
import ChatInput from '@/components/chat/ChatInput.vue'
import ChatMessage from '@/components/chat/ChatMessage.vue'
import WelcomePanel from '@/components/chat/WelcomePanel.vue'
import { isExamQuery } from '@/constants/examIntent'
import { shouldRenderReply, toAssistantMessage } from '@/lib/examFlow'
import { runExamQuery } from '@/lib/examFlow'
import { MOCK_REPLY_DELAY_MS, buildMockReply } from '@/lib/mockReply'
import { useConversationStore } from '@/stores/conversation'

const conversationStore = useConversationStore()

/** 侧边栏抽屉（仅窄屏有意义） */
const sidebarOpen = ref(false)

/** 在发送入口把文本写进 ChatInput 的 v-model */
const draft = ref('')

/** 助手「正在生成」状态：等待期间禁止重复发送 */
const pending = ref(false)

const inputRef = ref<InstanceType<typeof ChatInput> | null>(null)
const scrollRef = ref<HTMLElement | null>(null)

const messages = computed(() => conversationStore.messages)
const showWelcome = computed(() => !conversationStore.hasMessages)

/**
 * 真实请求的中断控制。
 *
 * 有了异步请求之后，「正在生成」不再只是一个定时器：如果用户清空会话、
 * 或在请求飞行中离开页面，回调再往 store 写消息就会把脏数据插到新上下文里。
 * 因此保留一个 controller，在两个位置主动取消（见下）。
 */
let inFlightController: AbortController | null = null

/** 消息变化后滚到底部（含占位回复出现的那一次） */
async function scrollToBottom() {
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => messages.value.length, scrollToBottom)
watch(pending, scrollToBottom)

/** 本地占位回复（非考试问题走这条，行为与 F2 完全一致） */
function replyWithMock(text: string) {
  window.setTimeout(() => {
    conversationStore.addMessage('assistant', buildMockReply(text), true)
    pending.value = false
    inputRef.value?.focus()
  }, MOCK_REPLY_DELAY_MS)
}

/**
 * 真实考试查询。
 *
 * 完成信号刻意**观察 assistant 消息数量变化**，而不是直接 `await` 编排函数：
 * 这样即便将来在同一个会话里并行发起了多个请求，也只会由「自己那一条」
 * assistant 消息的写入来解除 pending，不会互相把对方的等待状态清掉。
 */
function replyWithExams() {
  const controller = new AbortController()
  inFlightController = controller

  const beforeCount = conversationStore.messages.filter(
    (message) => message.role === 'assistant',
  ).length

  const stopWatch = watch(
    () =>
      conversationStore.messages.filter((message) => message.role === 'assistant').length >
      beforeCount,
    (arrived) => {
      if (!arrived) return
      stopWatch()
      pending.value = false
      nextTick(() => inputRef.value?.focus())
    },
  )

  void runExamQuery(controller.signal).then((payload) => {
    // 被取消（清空会话 / 离开页面）时不上屏，避免污染新上下文
    if (!shouldRenderReply(payload)) return
    conversationStore.appendMessage(toAssistantMessage(payload))
  })
}

function handleSend(text: string) {
  if (pending.value) return

  conversationStore.addMessage('user', text)
  pending.value = true

  if (isExamQuery(text)) {
    replyWithExams()
    return
  }

  replyWithMock(text)
}

/** 点击示例问题：只填入输入框并聚焦，不发送、不调用后端 */
function handleSelectPrompt(question: string) {
  draft.value = question
  nextTick(() => inputRef.value?.focus())
}

function handleNewConversation() {
  conversationStore.createConversation()
  inputRef.value?.focus()
}

/** 清空会话前先取消飞行中的请求，防止迟到的响应写进已清空的对话 */
function handleClearConversation() {
  inFlightController?.abort()
  inFlightController = null
  pending.value = false
  conversationStore.clearCurrentConversation()
  inputRef.value?.focus()
}

onMounted(() => {
  inputRef.value?.focus()
})

onBeforeUnmount(() => {
  inFlightController?.abort()
  inFlightController = null
})

/**
 * 调试入口（仅开发环境）：把 store 挂到 window，方便在控制台验证
 * 「新建对话 / 添加消息 / 重置」这些本轮被要求的操作。
 * F3 追加：把真实查询入口也挂上去，便于不点按钮就验证接口链路。
 */
if (import.meta.env.DEV) {
  const debugTarget = window as unknown as Record<string, unknown>
  debugTarget.__campusFlowConversationStore = conversationStore
  debugTarget.__campusFlowExamQuery = runExamQuery
}
</script>

<template>
  <div class="chat-page">
    <AppSidebar :open="sidebarOpen" @close="sidebarOpen = false" />

    <!-- 窄屏遮罩：点击关闭抽屉 -->
    <div
      v-if="sidebarOpen"
      class="chat-page__scrim"
      aria-hidden="true"
      @click="sidebarOpen = false"
    ></div>

    <main class="chat-page__main">
      <header class="chat-page__topbar">
        <button
          type="button"
          class="chat-page__menu"
          aria-label="打开对话列表"
          @click="sidebarOpen = !sidebarOpen"
        >
          ☰
        </button>
        <span class="chat-page__topbar-title">
          {{ conversationStore.currentConversation?.title ?? '新对话' }}
        </span>
        <button
          type="button"
          class="chat-page__clear"
          :disabled="showWelcome"
          @click="handleClearConversation"
        >
          清空
        </button>
      </header>

      <div ref="scrollRef" class="chat-page__scroll">
        <WelcomePanel v-if="showWelcome" @select-prompt="handleSelectPrompt" />

        <div v-else class="chat-page__messages">
          <ChatMessage
            v-for="message in messages"
            :key="message.id"
            :role="message.role"
            :content="message.content"
            :mock="message.mock"
            :error="message.error"
          />
          <ChatMessage v-if="pending" role="assistant" content="" pending />
        </div>
      </div>

      <ChatInput
        ref="inputRef"
        v-model="draft"
        :disabled="pending"
        @send="handleSend"
      />
    </main>
  </div>
</template>

<style scoped>
.chat-page {
  position: relative;
  display: flex;
  height: 100%;
  min-width: 0;
  overflow: hidden;
}

.chat-page__main {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  height: 100%;
  background: var(--cf-bg);
}

.chat-page__topbar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.625rem 1.5rem;
  border-bottom: 1px solid var(--cf-border);
}

.chat-page__menu {
  display: none;
  padding: 0.125rem 0.375rem;
  font: inherit;
  font-size: 0.9375rem;
  color: var(--cf-text-muted);
  background: transparent;
  border: 1px solid var(--cf-border);
  border-radius: 7px;
  cursor: pointer;
}

.chat-page__topbar-title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 0.8125rem;
  color: var(--cf-text-muted);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.chat-page__clear {
  flex: 0 0 auto;
  padding: 0.1875rem 0.5rem;
  font: inherit;
  font-size: 0.75rem;
  color: var(--cf-text-muted);
  background: transparent;
  border: 1px solid var(--cf-border);
  border-radius: 7px;
  cursor: pointer;
}

.chat-page__clear:disabled {
  color: var(--cf-text-subtle);
  cursor: not-allowed;
  opacity: 0.6;
}

.chat-page__scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  /* 超长内容只在本区域内换行/滚动，页面本身不出现横向滚动条 */
  overflow-x: hidden;
}

.chat-page__messages {
  padding: 1.5rem;
}

.chat-page__scrim {
  position: absolute;
  inset: 0;
  z-index: 10;
  background: transparent;
}

@media (max-width: 860px) {
  .chat-page__menu {
    display: inline-block;
  }

  .chat-page__topbar {
    padding: 0.625rem 1rem;
  }

  .chat-page__messages {
    padding: 1rem;
  }
}
</style>
