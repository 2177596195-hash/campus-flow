import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import type { ChatMessage, Conversation, MessageRole } from '@/types'

/**
 * 对话 store（纯前端本地状态）。
 *
 * 本阶段的边界：
 * - 只做本地状态：会话列表、当前会话、消息增删与「新对话」；
 * - **不发任何请求**（不 import axios / 不 import api 层），mock 回复由视图层显式写入；
 * - 数据结构保持扁平（见 `@/types/conversation`），不提前设计后端数据库模型。
 */

/** 短 id：仅前端 key 用途，不承担任何业务语义 */
function createId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/** 会话标题取自首条用户消息，超长截断 */
const TITLE_MAX_LENGTH = 18

function buildTitle(text: string): string {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (!normalized) return '新对话'
  return normalized.length > TITLE_MAX_LENGTH
    ? `${normalized.slice(0, TITLE_MAX_LENGTH)}…`
    : normalized
}

function createConversationRecord(): Conversation {
  const now = Date.now()
  return {
    id: createId(),
    title: '新对话',
    createdAt: now,
    updatedAt: now,
    messages: [],
  }
}

export const useConversationStore = defineStore('conversation', () => {
  /** 全部会话，最新的排在最前 */
  const conversations = ref<Conversation[]>([])
  const currentConversationId = ref<string | null>(null)

  /** 当前会话对象；无会话时为 null */
  const currentConversation = computed<Conversation | null>(() =>
    conversations.value.find((item) => item.id === currentConversationId.value) ?? null,
  )

  /** 当前会话的消息列表（无会话时为空数组，视图可直接遍历） */
  const messages = computed<ChatMessage[]>(() => currentConversation.value?.messages ?? [])

  const hasMessages = computed(() => messages.value.length > 0)

  const conversationCount = computed(() => conversations.value.length)

  function upsertCurrentConversation(): Conversation {
    const existing = currentConversation.value
    if (existing) return existing

    const created = createConversationRecord()
    conversations.value.unshift(created)
    currentConversationId.value = created.id
    return created
  }

  /**
   * 新建会话并切换过去。
   *
   * 若当前会话还是「空会话」（一条消息都没有），则**直接复用它**而不是再开一个，
   * 避免用户连点「新对话」堆出一串空记录 —— 这是唯一一处刻意保留的交互细节。
   */
  function createConversation(): Conversation {
    const current = currentConversation.value
    if (current && current.messages.length === 0) return current

    const created = createConversationRecord()
    conversations.value.unshift(created)
    currentConversationId.value = created.id
    return created
  }

  /** 添加一条消息；没有当前会话时自动开一个 */
  function addMessage(
    role: MessageRole,
    content: string,
    mock = false,
    error = false,
  ): ChatMessage {
    const conversation = upsertCurrentConversation()
    const message: ChatMessage = {
      id: createId(),
      role,
      content,
      createdAt: Date.now(),
      ...(mock ? { mock: true } : {}),
      ...(error ? { error: true } : {}),
    }

    conversation.messages.push(message)
    conversation.updatedAt = message.createdAt

    // 首条用户消息决定会话标题，之后不再改动（保住用户对列表的稳定认知）。
    if (role === 'user' && conversation.title === '新对话') {
      conversation.title = buildTitle(content)
    }

    return message
  }

  /**
   * 直接追加一条**已经构造好**的消息。
   *
   * 存在的理由：真实请求（如考试查询）的结果来自 async 编排层，
   * 那里已经确定了 role / content / 标记位，不希望再按位置参数一个个往下传
   * （`addMessage(role, content, mock, error)` 一旦再加标记就会失控）。
   * 仍然复用同一套「自动开会话 + 首条用户消息定标题」逻辑，行为与 `addMessage` 一致。
   */
  function appendMessage(message: Pick<ChatMessage, 'role' | 'content' | 'mock' | 'error'>): ChatMessage {
    return addMessage(message.role, message.content, message.mock ?? false, message.error ?? false)
  }

  /** 切换当前会话（不存在的 id 直接忽略） */
  function selectConversation(id: string): void {
    if (conversations.value.some((item) => item.id === id)) {
      currentConversationId.value = id
    }
  }

  /** 删除会话；删的是当前会话时退到列表里的下一条 */
  function removeConversation(id: string): void {
    const index = conversations.value.findIndex((item) => item.id === id)
    if (index === -1) return

    conversations.value.splice(index, 1)
    if (currentConversationId.value !== id) return

    const fallback = conversations.value[index] ?? conversations.value[index - 1] ?? null
    currentConversationId.value = fallback ? fallback.id : null
  }

  /** 清空当前会话的消息，但保留这条会话 */
  function clearCurrentConversation(): void {
    const conversation = currentConversation.value
    if (!conversation) return

    conversation.messages.splice(0, conversation.messages.length)
    conversation.title = '新对话'
    conversation.updatedAt = Date.now()
  }

  /** 全部重置：清空会话列表并回到「无会话」状态 */
  function reset(): void {
    conversations.value.splice(0, conversations.value.length)
    currentConversationId.value = null
  }

  return {
    conversations,
    currentConversationId,
    currentConversation,
    messages,
    hasMessages,
    conversationCount,
    createConversation,
    addMessage,
    appendMessage,
    selectConversation,
    removeConversation,
    clearCurrentConversation,
    reset,
  }
})
