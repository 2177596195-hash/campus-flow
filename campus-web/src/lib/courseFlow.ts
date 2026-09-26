/**
 * 「课程表查询」这条真实流程的编排。
 *
 * 与 `examFlow` 保持同一套职责边界，两边刻意互不依赖：
 * 让组件只负责「什么时候触发、结果怎么显示」，把「调哪个接口、
 * 文案怎么拼、错误怎么归一」留在可被单独测试的纯函数里；
 * 等接入 Agent 后，只需要替换本模块的内部实现。
 *
 * 与 `examFlow` 的唯一结构性差异：**周次是显式入参**。
 * 「查第几周」是课程查询的业务参数（考试查询没有对应物），
 * 因此 `runCourseQuery` 的第一个参数就是 `week`——
 * 组件层从用户输入里抽到就传数字、抽不到就传 undefined（整学期），
 * 本模块绝不做任何「默认当前周」之类的猜测。
 *
 * 返回值刻意设计成**单个消息载荷**：一次调用只产生一条 assistant 消息，
 * 成功是真实数据、失败是明确提示 —— 两者都不会产出「编造的课程数据」。
 */

import { CourseApiError, fetchCourseSchedules, formatCourseSchedules } from '@/api/courses'
import { resolveStudentId } from '@/config/env'
import type { ChatMessage } from '@/types'

/** 一条待写入聊天区的 assistant 消息载荷 */
export interface CourseReplyPayload {
  role: 'assistant'
  content: string
  /** 失败时为 true，界面上会显示「失败」标记，与正常回答区分开 */
  error?: boolean
}

/**
 * 执行一次课程表查询，返回可直接写入 store 的消息载荷。
 *
 * @param week 教学周序号：明确数字 = 只查该周；undefined = 整学期课表。
 *             **必须由调用方显式传入**（组件层从输入抽取），本函数不提供默认值。
 * @param signal 可选的中断信号（组件卸载 / 清空会话 / 重复发送时取消）
 *
 * **本函数从不抛异常**：所有失败都被归一成一条明确的失败提示，
 * 保证调用方（组件）只需要处理「有结果」这一种情况。
 */
export async function runCourseQuery(
  week: number | undefined,
  signal?: AbortSignal,
): Promise<CourseReplyPayload> {
  const studentId = resolveStudentId()

  try {
    const courses = await fetchCourseSchedules({ studentId, week }, signal)
    // 空数组走这里：它是正常结果（该周没课），因此不加 error 标记
    return { role: 'assistant', content: formatCourseSchedules(studentId, courses, week) }
  } catch (error) {
    // 用户主动取消（组件卸载 / 清空会话 / 重新发送）：不产生任何消息
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { role: 'assistant', content: '', error: true }
    }

    const message =
      error instanceof CourseApiError
        ? error.message
        : '课表查询失败，请稍后重试'

    return { role: 'assistant', content: message, error: true }
  }
}

/** 失败载荷是否"应当展示"（取消导致的空载荷不上屏） */
export function shouldRenderReply(payload: CourseReplyPayload): boolean {
  return payload.content.trim().length > 0
}

/** 课程载荷到 `ChatMessage` 的适配（store 只关心 role/content/mock/error） */
export function toAssistantMessage(payload: CourseReplyPayload): Pick<
  ChatMessage,
  'role' | 'content' | 'error'
> {
  return {
    role: 'assistant',
    content: payload.content,
    ...(payload.error ? { error: true } : {}),
  }
}
