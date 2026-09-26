/**
 * 「请假申请」流程编排（F3-4）。
 *
 * 与 `scoreFlow` 的多轮追问同款思路，但更复杂：
 * - 成绩只需追问一个字段（学期），请假需要追问三个（startTime / endTime / reason）；
 * - 成绩追问是「整句固定文案」，请假追问需要根据已收集到的字段动态生成；
 * - 请假需要从自然语言里解析日期（「10月12日」「明天」）和事由（「因为感冒」）。
 *
 * 设计原则：
 * - **不猜测**：用户没提供的信息绝不编造，缺什么追问什么；
 * - **不抛异常**：所有失败都被归一成消息载荷，调用方只需处理「有结果」一种情况；
 * - **纯函数优先**：日期解析、追问生成都是纯函数，便于测试与验证。
 */

import { LeaveApiError, formatLeaveResult, submitLeaveApplication } from '@/api/leaves'
import { resolveStudentId } from '@/config/env'
import type { LeaveCollectedData, LeaveResponse } from '@/types/leave'
import type { ChatMessage } from '@/types'

/** 一条待写入聊天区的 assistant 消息载荷 */
export interface LeaveReplyPayload {
  role: 'assistant'
  content: string
  error?: boolean
}

// ─── 日期解析 ────────────────────────────────────────────────────────────

/** 日期解析结果：后端格式串 + 在原文中的位置（用于后续剥离） */
interface ParsedDate {
  formatted: string
  start: number
  end: number
}

/** 把 Date 格式化为后端接受的 `YYYY-MM-DD HH:MM` */
function formatBackendDate(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  const h = String(date.getHours()).padStart(2, '0')
  const min = String(date.getMinutes()).padStart(2, '0')
  return `${y}-${m}-${d} ${h}:${min}`
}

/** 取今天的零点（用于相对日期计算） */
function today(): Date {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

/** 把两个日期部分（date + time）合成一个后端格式串 */
function combineDateAndTime(date: Date, time: { hours: number; minutes: number }): string {
  const result = new Date(date)
  result.setHours(time.hours, time.minutes, 0, 0)
  return formatBackendDate(result)
}

/**
 * 从文本里抽取所有日期表达式。
 *
 * 支持：
 * - ISO 格式：`2026-10-12T08:00`、`2026-10-12 08:00`、`2026-10-12`
 * - 中文格式：`10月12日 8:00`、`10月12号上午8点`
 * - 相对日期：`今天`、`明天`、`后天`
 *
 * 返回按文本位置排序的结果列表。
 */
function extractDates(text: string): ParsedDate[] {
  const results: ParsedDate[] = []

  // 1) ISO 日期时间：2026-10-12T08:00 或 2026-10-12 08:00
  const isoDateTimeRe = /\d{4}-(\d{1,2})-(\d{1,2})[T\s](\d{1,2}):(\d{2})/g
  for (const m of text.matchAll(isoDateTimeRe)) {
    const month = Number(m[1])
    const day = Number(m[2])
    const hours = Number(m[3])
    const minutes = Number(m[4])
    if (month < 1 || month > 12 || day < 1 || day > 31 || hours > 23 || minutes > 59) continue
    const year = Number(m[0].match(/^\d{4}/)![0])
    const date = new Date(year, month - 1, day, hours, minutes, 0, 0)
    results.push({ formatted: formatBackendDate(date), start: m.index!, end: m.index! + m[0].length })
  }

  // 2) ISO 纯日期：2026-10-12（排除已被上一步匹配的）
  const isoDateRe = /\d{4}-(\d{1,2})-(\d{1,2})(?![T\s\d])/g
  for (const m of text.matchAll(isoDateRe)) {
    if (results.some((r) => m.index! >= r.start && m.index! < r.end)) continue
    const month = Number(m[1])
    const day = Number(m[2])
    if (month < 1 || month > 12 || day < 1 || day > 31) continue
    const year = Number(m[0].match(/^\d{4}/)![0])
    const date = new Date(year, month - 1, day, 0, 0, 0, 0)
    results.push({ formatted: formatBackendDate(date), start: m.index!, end: m.index! + m[0].length })
  }

  // 3) 中文日期时间：10月12日/号 8:00 或 10月12日/号上午8点
  const cnDateTimeRe = /(\d{1,2})月(\d{1,2})[日号]\s*(?:([上下]午)\s*)?(\d{1,2})[点:：](\d{1,2})?/g
  for (const m of text.matchAll(cnDateTimeRe)) {
    const month = Number(m[1])
    const day = Number(m[2])
    if (month < 1 || month > 12 || day < 1 || day > 31) continue
    let hours = Number(m[4])
    const minutes = m[5] ? Number(m[5]) : 0
    const period = m[3]
    if (period === '下午' && hours < 12) hours += 12
    if (period === '上午' && hours === 12) hours = 0
    if (hours > 23 || minutes > 59) continue
    const t = today()
    t.setMonth(month - 1, day)
    const date = new Date(t)
    date.setHours(hours, minutes, 0, 0)
    results.push({ formatted: formatBackendDate(date), start: m.index!, end: m.index! + m[0].length })
  }

  // 4) 中文纯日期：10月12日/号（排除已被上一步匹配的）
  const cnDateRe = /(\d{1,2})月(\d{1,2})[日号](?!\s*(?:[上下]午)?\s*\d)/g
  for (const m of text.matchAll(cnDateRe)) {
    if (results.some((r) => m.index! >= r.start && m.index! < r.end)) continue
    const month = Number(m[1])
    const day = Number(m[2])
    if (month < 1 || month > 12 || day < 1 || day > 31) continue
    const t = today()
    const date = new Date(t.getFullYear(), month - 1, day, 0, 0, 0, 0)
    results.push({ formatted: formatBackendDate(date), start: m.index!, end: m.index! + m[0].length })
  }

  // 5) 相对日期：今天、明天、后天
  const relativeRe = /今天|明天|后天/g
  for (const m of text.matchAll(relativeRe)) {
    const t = today()
    const offset = m[0] === '明天' ? 1 : m[0] === '后天' ? 2 : 0
    t.setDate(t.getDate() + offset)
    results.push({ formatted: formatBackendDate(t), start: m.index!, end: m.index! + m[0].length })
  }

  results.sort((a, b) => a.start - b.start)
  return results
}

/**
 * 从文本里抽取请假事由。
 *
 * 策略：
 * 1. 先剥离所有日期表达式（用空格替换），避免日期数字被误当原因；
 * 2. 查找原因指示词（因为、由于、原因是），取其后的文本；
 * 3. 没有指示词时，剥离意图关键词后，如果剩余文本够长（>4 字符），视为原因。
 */
function extractReason(text: string): string | undefined {
  const dates = extractDates(text)
  let remaining = text
  for (const date of [...dates].reverse()) {
    remaining = remaining.slice(0, date.start) + ' ' + remaining.slice(date.end)
  }
  remaining = remaining.replace(/[，,。.、；;！!？?\s]+/g, ' ').trim()

  const reasonIndicatorRe = /(?:因为|由于|原因是?|事由是?)[：:\s]*(.+)/
  const indicatorMatch = remaining.match(reasonIndicatorRe)
  if (indicatorMatch?.[1]) {
    const reason = indicatorMatch[1].trim()
    if (reason.length > 0) return reason
  }

  const intentPhrases = [
    '我要请假', '我想请假', '帮我请假', '申请请假', '提交请假',
    '我要请个假', '我想请个假', '帮我请个假', '请事假', '请病假',
    '我要休假', '我想休假', '申请休假', '请假',
  ]
  let stripped = remaining
  for (const phrase of intentPhrases) {
    stripped = stripped.replace(phrase, '')
  }
  stripped = stripped.replace(/[，,。.、；;！!？?\s]+/g, ' ').trim()

  if (stripped.length > 4) return stripped
  return undefined
}

/**
 * 从用户输入里解析请假信息，合并到已收集的数据上。
 *
 * 日期按出现顺序依次填充 startTime → endTime（不够的不填、多出的忽略）。
 * 事由只在 reason 尚未收集时尝试提取。
 *
 * @returns 新的已收集数据（不修改入参）
 */
export function parseLeaveInput(
  text: string,
  existing: LeaveCollectedData,
): LeaveCollectedData {
  const result = { ...existing }
  const dates = extractDates(text)

  let dateIndex = 0
  if (!result.startTime && dateIndex < dates.length) {
    result.startTime = dates[dateIndex]!.formatted
    dateIndex++
  }
  if (!result.endTime && dateIndex < dates.length) {
    result.endTime = dates[dateIndex]!.formatted
    dateIndex++
  }

  if (!result.reason) {
    const reason = extractReason(text)
    if (reason) result.reason = reason
  }

  return result
}

// ─── 追问生成 ────────────────────────────────────────────────────────────

/** 判断已收集的数据是否满足提交条件 */
export function isLeaveComplete(data: LeaveCollectedData): boolean {
  return Boolean(data.startTime && data.endTime && data.reason)
}

/**
 * 根据当前已收集到的信息，生成下一步追问。
 *
 * 追问顺序：startTime → endTime → reason。
 * 已收集的字段不再追问，缺什么问什么。
 * 全部收集完返回 undefined（表示可以提交了）。
 */
export function getLeaveFollowup(data: LeaveCollectedData): string | undefined {
  if (!data.startTime) {
    return [
      '好的，帮你提交请假申请。还需要以下信息：',
      '',
      '请告诉我请假**开始时间**，例如：',
      '- 10月12日 8:00',
      '- 2026-10-12 08:00',
      '- 明天',
    ].join('\n')
  }

  if (!data.endTime) {
    return [
      `收到，开始时间：${data.startTime}`,
      '',
      '请告诉我请假**结束时间**，例如：',
      '- 10月14日 18:00',
      '- 2026-10-14 18:00',
      '- 后天',
    ].join('\n')
  }

  if (!data.reason) {
    return '收到，结束时间：' + data.endTime + '\n\n请告诉我请假**事由**（原因），例如：感冒就医、家中有事等。'
  }

  return undefined
}

// ─── 提交与格式化 ────────────────────────────────────────────────────────

/**
 * 提交请假申请，返回可写入 store 的消息载荷。
 *
 * **本函数从不抛异常**：所有失败都被归一成一条明确的失败提示。
 */
export async function submitLeave(
  data: LeaveCollectedData,
  signal?: AbortSignal,
): Promise<LeaveReplyPayload> {
  if (!isLeaveComplete(data)) {
    return { role: 'assistant', content: '请假信息不完整，无法提交。', error: true }
  }

  const studentId = resolveStudentId()

  try {
    const response = await submitLeaveApplication(
      {
        studentId,
        startTime: data.startTime!,
        endTime: data.endTime!,
        reason: data.reason!,
      },
      signal,
    )
    return { role: 'assistant', content: formatLeaveResult(response) }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { role: 'assistant', content: '', error: true }
    }

    const message =
      error instanceof LeaveApiError
        ? error.message
        : '请假提交失败，请稍后重试'

    return { role: 'assistant', content: message, error: true }
  }
}

// ─── 载荷适配 ────────────────────────────────────────────────────────────

/** 失败载荷是否"应当展示"（取消导致的空载荷不上屏） */
export function shouldRenderReply(payload: LeaveReplyPayload): boolean {
  return payload.content.trim().length > 0
}

/** 请假载荷到 `ChatMessage` 的适配 */
export function toAssistantMessage(payload: LeaveReplyPayload): Pick<
  ChatMessage,
  'role' | 'content' | 'error'
> {
  return {
    role: 'assistant',
    content: payload.content,
    ...(payload.error ? { error: true } : {}),
  }
}
