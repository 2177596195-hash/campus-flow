/**
 * 「经 Agent 的成绩查询」编排层。
 *
 * 与 F3 的 `@/lib/scoreFlow.ts` 的核心区别：
 * - F3：前端解析学期 → 直调 Spring Boot → 前端格式化数据
 * - F4（本模块）：前端发送原文 → Agent 全权处理（意图识别、槽位填充、工具调用、
 *   自然语言生成）→ 前端只展示 Agent 的回复
 *
 * 多轮对话靠 `threadId` 维持：Agent 的 checkpointer 按 threadId 保存会话状态，
 * 因此「追问学期 → 用户补学期 → 查询」这个多轮流程完全由 Agent 内部处理，
 * 前端只需把同一 threadId 带回去。
 *
 * F3 的 `scoreFlow.ts` 保留不动，作为直连 Spring Boot 的备用链路。
 */

import { AgentApiError, sendAgentChat, type AgentErrorKind } from '@/api/agent'

/** Agent 成绩对话的回复载荷 */
export interface AgentScoreReply {
  role: 'assistant'
  content: string
  error?: boolean
}

/**
 * 一轮 Agent 成绩对话的结果。
 *
 * 字段说明（接线层只依赖这三个）：
 * - ``reply``：要展示的 assistant 载荷（失败时 `error` 为 true，`content` 是中文提示）；
 * - ``threadId``：本次使用的会话标识，下一轮原样带回（多轮靠它）；
 * - ``failureKind``：**失败分类**，成功时不存在。取值来自 `@/api/agent` 的
 *   ``AgentErrorKind``（network / http / malformed / timeout），
 *   让调用方能「按类型区分失败」而不必去匹配错误文案。
 */
export interface AgentScoreTurn {
  reply: AgentScoreReply
  threadId: string
  failureKind?: AgentErrorKind
}

/**
 * 经 Agent 执行一轮成绩相关对话。
 *
 * @param message 用户原文（例如「查看成绩」「2025-2026-1 学期成绩」）
 * @param threadId 会话标识（多轮时传入上一轮返回的 threadId）
 * @param signal 可选的中断信号
 *
 * 返回：{ reply, threadId, failureKind }
 * - reply 是 Agent 的自然语言回答（可能是追问学期，也可能是成绩汇总）
 * - threadId 是本次使用的会话标识，下一轮原样带回
 * - failureKind 仅在失败时出现
 *
 * 本函数从不抛异常：所有失败归成一条明确的失败提示（并带上分类）。
 */
export async function runAgentScoreChat(
  message: string,
  threadId?: string,
  signal?: AbortSignal,
): Promise<AgentScoreTurn> {
  try {
    const result = await sendAgentChat(message, threadId, signal)
    return {
      reply: { role: 'assistant', content: result.reply },
      threadId: result.threadId,
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { reply: { role: 'assistant', content: '', error: true }, threadId: threadId ?? '' }
    }

    if (error instanceof AgentApiError) {
      return {
        reply: { role: 'assistant', content: error.message, error: true },
        threadId: threadId ?? '',
        failureKind: error.kind,
      }
    }

    return {
      reply: { role: 'assistant', content: '智能助手暂时无法回答，请稍后重试', error: true },
      threadId: threadId ?? '',
      failureKind: 'network',
    }
  }
}

/** 载荷是否应当展示（取消导致的空载荷不上屏） */
export function shouldRenderAgentReply(reply: AgentScoreReply): boolean {
  return reply.content.trim().length > 0
}

/** Agent 回复到 ChatMessage 的适配 */
export function toAgentAssistantMessage(reply: AgentScoreReply): Pick<
  import('@/types').ChatMessage,
  'role' | 'content' | 'error'
> {
  return {
    role: 'assistant',
    content: reply.content,
    ...(reply.error ? { error: true } : {}),
  }
}
