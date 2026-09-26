/**
 * 「成绩查询」这条真实流程的编排。
 *
 * 与 `examFlow` / `courseFlow` 保持同一套职责边界，三方刻意互不依赖：
 * 让组件只负责「什么时候触发、结果怎么显示」，把「调哪个接口、
 * 文案怎么拼、错误怎么归一」留在可被单独测试的纯函数里；
 * 等接入 Agent 后，只需要替换本模块的内部实现。
 *
 * 与 `examFlow` 的唯一结构性差异：**学期是显式入参**。
 * 与 `courseFlow` 的差异有两点：
 * 1. `semester` 是**必答的业务参数**——组件层若没从输入里解析出学期
 *    （用户也没明确说查全部），本轮**不调接口**，只回一句追问
 *    （见 `SCORE_SEMESTER_FOLLOWUP`），下一轮再补；
 * 2. `semester: undefined` 有**双重含义**：用户明确说「全部 / 所有学期」
 *    （`wantsAllSemesters` 命中）时它表示「查全部」；这个区分发生在
 *    意图层（`ChatView` 的槽位逻辑），到达本模块时只剩
 *    「有学期就按学期查、没有就查全部」一条规则。
 *
 * 返回值刻意设计成**单个消息载荷**：一次调用只产生一条 assistant 消息，
 * 成功是真实数据、失败是明确提示 —— 两者都不会产出「编造的成绩数据」。
 */

import { ScoreApiError, fetchScoreRecords, formatScoreRecords } from '@/api/scores'
import { resolveStudentId } from '@/config/env'
import type { ChatMessage } from '@/types'

/** 一条待写入聊天区的 assistant 消息载荷 */
export interface ScoreReplyPayload {
  role: 'assistant'
  content: string
  /** 失败时为 true，界面上会显示「失败」标记，与正常回答区分开 */
  error?: boolean
}

/**
 * 用户没有明确给出学期时的**追问文案**。
 *
 * 这是前端本地的最小「任务槽位」：本轮只产生这一句追问、不发任何请求，
 * 等用户下一轮补上学期（或明确说「全部」）后再真正查询。
 * （Python Agent 的 `task_slot` 机制不动，这里只是前端可用的最小等价物。）
 */
export const SCORE_SEMESTER_FOLLOWUP = [
  '想帮你查询成绩，还差一个信息：要查哪个学期？',
  '',
  '可以这样告诉我：',
  '- 直接说学期，例如「2025-2026-1」或「2025-2026 第一学期」；',
  '- 回复「全部」，查询所有学期的成绩。',
].join('\n')

/**
 * 执行一次成绩查询，返回可直接写入 store 的消息载荷。
 *
 * @param semester 学期（后端格式，如 `2025-2026-1`）；
 *                 **必须由调用方显式传入**——要么是用户明确写出的学期，
 *                 要么是用户明确选择「全部」时的 undefined。
 *                 本函数不提供默认值、不做任何猜测。
 * @param signal 可选的中断信号（组件卸载 / 清空会话 / 重复发送时取消）
 *
 * **本函数从不抛异常**：所有失败都被归一成一条明确的失败提示，
 * 保证调用方（组件）只需要处理「有结果」这一种情况。
 */
export async function runScoreQuery(
  semester: string | undefined,
  signal?: AbortSignal,
): Promise<ScoreReplyPayload> {
  const studentId = resolveStudentId()

  try {
    const scores = await fetchScoreRecords({ studentId, semester }, signal)
    // 空数组走这里：它是正常结果（该学期没出分），因此不加 error 标记
    return { role: 'assistant', content: formatScoreRecords(studentId, scores, semester) }
  } catch (error) {
    // 用户主动取消（组件卸载 / 清空会话 / 重新发送）：不产生任何消息
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { role: 'assistant', content: '', error: true }
    }

    const message =
      error instanceof ScoreApiError
        ? error.message
        : '成绩查询失败，请稍后重试'

    return { role: 'assistant', content: message, error: true }
  }
}

/** 失败载荷是否"应当展示"（取消导致的空载荷不上屏） */
export function shouldRenderReply(payload: ScoreReplyPayload): boolean {
  return payload.content.trim().length > 0
}

/** 成绩载荷到 `ChatMessage` 的适配（store 只关心 role/content/mock/error） */
export function toAssistantMessage(payload: ScoreReplyPayload): Pick<
  ChatMessage,
  'role' | 'content' | 'error'
> {
  return {
    role: 'assistant',
    content: payload.content,
    ...(payload.error ? { error: true } : {}),
  }
}
