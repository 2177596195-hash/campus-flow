/**
 * 对话领域的前端本地类型。
 *
 * 注意：这里刻意只描述「前端渲染一屏聊天需要什么」，不是后端数据库模型。
 * 后续接入 Agent API 时，由 adapter 把服务端返回映射到这些类型，不改组件。
 */

/** 消息角色。assistant 与 user 之外不预留额外角色，避免过早抽象。 */
export type MessageRole = 'user' | 'assistant'

/** 一条聊天消息 */
export interface ChatMessage {
  id: string
  role: MessageRole
  /** 纯文本内容；渲染时按空白与换行保留 */
  content: string
  /** 毫秒时间戳，用于展示与排序 */
  createdAt: number
  /** 本地占位回复标记（本阶段 mock 来源，接入真实 Agent 前用于 UI 提示） */
  mock?: boolean
}

/** 一个会话 */
export interface Conversation {
  id: string
  /** 会话标题：由首条用户消息截断生成，未发言时为「新对话」 */
  title: string
  createdAt: number
  updatedAt: number
  messages: ChatMessage[]
}
