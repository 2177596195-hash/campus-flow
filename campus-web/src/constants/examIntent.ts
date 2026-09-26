/**
 * 「考试安排」意图识别（前端本地、最小实现）。
 *
 * 这是一个**刻意的临时件**：F3 阶段不接 Python Agent、不接 LangGraph，
 * 但需要让「点击示例问题 → 真实 API → 聊天区」这条链路能跑通，
 * 所以先在视图层前面放一层极简的关键词判定。
 *
 * 边界（重要）：
 * - 只判断**是不是考试相关**，不做参数抽取（学号由 `@/config/env` 固定提供）；
 * - 独立成模块 + 纯函数，等 Agent 接入后**整文件删除**即可，
 *   调用方（`ChatView`）只需删掉一次分支判断，不需要改动组件结构。
 */

/**
 * 命中关键词即视为考试意图。
 *
 * 选取原则：覆盖"考试"这件事的常见说法（考试 / 备考 / 期中期末 / 考场），
 * 但**刻意避开**「安排」这类过于宽泛的词（否则「安排一下课表」会被误判）。
 */
const EXAM_KEYWORDS = [
  '考试',
  '考表',
  '考场',
  '考期',
  '补考',
  '重修考试',
  '期中',
  '期末',
  '几号考',
  '什么时候考',
] as const

/**
 * 判断用户输入是否在问考试安排。
 *
 * 纯函数：去掉所有空白后做包含匹配，与 `mockReply` 的匹配口径保持一致。
 */
export function isExamQuery(text: string): boolean {
  const normalized = text.replace(/\s+/g, '')
  if (!normalized) return false

  return EXAM_KEYWORDS.some((keyword) => normalized.includes(keyword))
}
