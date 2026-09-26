/**
 * 「课程表」意图识别（前端本地、最小实现）。
 *
 * 这是一个**刻意的临时件**（与 `examIntent` 同款）：F3 阶段不接 Python Agent、
 * 不接 LangGraph，但需要让「输入课程问题 → 真实 API → 聊天区」这条链路能跑通，
 * 所以先在视图层前面放一层极简的关键词判定。
 *
 * 边界（重要）：
 * - 只判断**是不是课程相关**，学号由 `@/config/env` 固定提供；
 * - **不做周次猜测**：抽取周次的函数（`parseCourseWeek`）抽不到就返回
 *   undefined，由调用方按「整学期」处理——前端没有学期日历，
 *   「这周 = 第几周」这种映射猜了就是编造；
 * - 与考试关键词**刻意互斥**：两边词表都不含对方领域的字眼
 *   （考试侧刻意避开「安排 / 课表」，课程侧刻意不含「考」），
 *   因此分流顺序不影响结果，但 `ChatView` 仍先判考试、再判课程，
 *   保证 F3-1 行为分毫不改；
 * - 独立成模块 + 纯函数，等 Agent 接入后**整文件删除**即可。
 */

/**
 * 命中关键词即视为课程意图。
 *
 * 选取原则：覆盖「上课 / 课表」这件事的常见说法，同时避开单字「课」
 * 这类过宽的匹配（「下课了吗」「选课」不是查课表）。
 */
const COURSE_KEYWORDS = [
  '课表',
  '课程',
  '上课',
  '有课',
  '没课',
  '什么课',
  '哪些课',
  '几门课',
] as const

/**
 * 判断用户输入是否在问课程安排。
 *
 * 纯函数：去掉所有空白后做包含匹配，与 `examIntent` / `mockReply` 的口径一致。
 */
export function isCourseQuery(text: string): boolean {
  const normalized = text.replace(/\s+/g, '')
  if (!normalized) return false

  return COURSE_KEYWORDS.some((keyword) => normalized.includes(keyword))
}

/**
 * 「第 N 周」的匹配模式：数字两侧允许空白，1~2 位数字。
 *
 * 只认这一种**明确**写法，是刻意的保守策略（宁缺毋滥）：
 * - 「查看第3周课程」「第 13 周」「第 8 周的课表」都能取到；
 * - 三位及以上（如「第100周」）不匹配 → 当作未指定周次；
 * - 「这周 / 本周 / 下周 / 第几周」没有数字 → 同样返回 undefined。
 */
const WEEK_PATTERN = /第\s*(\d{1,2})\s*周/

/**
 * 从用户输入里抽取「第 N 周」的周次。
 *
 * @returns 明确写出的周次数字；用户没有指定时返回 **undefined**
 *          （注意：不是 0、不是 1、不是「当前周」——前端没有依据就不猜）。
 *          一句话里出现多个周次时只取第一个（本阶段一次只查一周）。
 */
export function parseCourseWeek(text: string): number | undefined {
  const match = text.match(WEEK_PATTERN)
  if (!match) return undefined

  const week = Number.parseInt(match[1] ?? '', 10)
  return Number.isFinite(week) ? week : undefined
}
