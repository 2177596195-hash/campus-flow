/**
 * 「请假申请」意图识别（前端本地、最小实现）。
 *
 * 与 `scoreIntent` / `examIntent` / `courseIntent` 同款临时件：
 * F3-4 阶段不接 Python Agent，但需要让「请假 → POST /api/leaves」跑通。
 *
 * 边界：
 * - 只判断**是不是请假意图**，学号由 `@/config/env` 固定提供；
 * - 不解析日期、不猜测请假事由——解析职责在 `leaveFlow` 模块；
 * - 独立成模块，等 Agent 接入后**整文件删除**即可。
 */

const LEAVE_KEYWORDS = [
  '请假',
  '请个假',
  '申请请假',
  '申请请假',
  '提交请假',
  '请事假',
  '请病假',
  '休假',
  '申请休假',
] as const

/**
 * 判断用户输入是否在申请请假。
 *
 * 纯函数：去掉所有空白后做包含匹配，与其它意图模块口径一致。
 */
export function isLeaveQuery(text: string): boolean {
  const normalized = text.replace(/\s+/g, '')
  if (!normalized) return false

  return LEAVE_KEYWORDS.some((keyword) => normalized.includes(keyword))
}
