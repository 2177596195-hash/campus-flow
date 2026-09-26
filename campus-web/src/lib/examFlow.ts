/**
 * 「考试安排查询」这一条真实流程的编排。
 *
 * 为什么单独成模块而不是写在 `ChatView` 里：
 * 让组件只负责「什么时候触发、结果怎么显示」，把「调哪个接口、文案怎么拼、
 * 错误怎么归一」留在可被单独测试的纯函数里；
 * 等接入 Agent 后，只需要替换本模块的内部实现。
 *
 * 返回值刻意设计成**单个消息载荷**：一次调用只产生一条 assistant 消息，
 * 成功是真实数据、失败是明确提示 —— 两者都不会产出「编造的考试数据」。
 */

import { ExamApiError, fetchExamSchedules, formatExamSchedules } from '@/api/exams'
import { resolveStudentId } from '@/config/env'
import type { ChatMessage } from '@/types'

/** 一条待写入聊天区的 assistant 消息载荷 */
export interface ExamReplyPayload {
  role: 'assistant'
  content: string
  /** 失败时为 true，界面上会显示「失败」标记，与正常回答区分开 */
  error?: boolean
}

/**
 * 执行一次考试安排查询，返回可直接写入 store 的消息载荷。
 *
 * **本函数从不抛异常**：所有失败都被归一成一条明确的失败提示，
 * 保证调用方（组件）只需要处理「有结果」这一种情况。
 */
export async function runExamQuery(signal?: AbortSignal): Promise<ExamReplyPayload> {
  const studentId = resolveStudentId()

  try {
    const exams = await fetchExamSchedules({ studentId }, signal)
    // 空数组走这里：它是正常结果，因此不加 error 标记
    return { role: 'assistant', content: formatExamSchedules(studentId, exams) }
  } catch (error) {
    // 用户主动取消（组件卸载 / 重新发送）：不产生任何消息
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { role: 'assistant', content: '', error: true }
    }

    const message =
      error instanceof ExamApiError
        ? error.message
        : '考试安排查询失败，请稍后重试'

    return { role: 'assistant', content: message, error: true }
  }
}

/** 失败载荷是否"应当展示"（取消导致的空载荷不上屏） */
export function shouldRenderReply(payload: ExamReplyPayload): boolean {
  return payload.content.trim().length > 0
}

/** 占位/错误载荷到 `ChatMessage` 的适配（store 只关心 role/content/mock/error） */
export function toAssistantMessage(payload: ExamReplyPayload): Pick<
  ChatMessage,
  'role' | 'content' | 'error'
> {
  return {
    role: 'assistant',
    content: payload.content,
    ...(payload.error ? { error: true } : {}),
  }
}
