/**
 * 「成绩查询」意图识别与学期解析（前端本地、最小实现）。
 *
 * 这是一个**刻意的临时件**（与 `examIntent` / `courseIntent` 同款）：
 * F3 阶段不接 Python Agent、不接 LangGraph，但需要让
 * 「输入成绩问题 → 真实 API → 聊天区」这条链路能跑通，
 * 所以先在视图层前面放一层极简的关键词判定 + 参数抽取。
 *
 * 边界（重要）：
 * - 只判断**是不是成绩相关**，学号由 `@/config/env` 固定提供；
 * - **不做学期猜测**：`parseSemester` 只认「明确写出来」的学期
 *   （两种写法，见下）；「这学期 / 上学期 / 第一学期（没写年份）」
 *   一律返回 undefined，由调用方追问——前端没有学期日历，
 *   「上学期 = 2025-2026-1」这种映射猜了就是编造
 *   （与 `courseIntent` 不猜「这周 = 第几周」同一策略）；
 * - **刻意放在考试 / 课程意图之前判定**：成绩词表里的「成绩 / 分数」
 *   比考试词表更具体，「看看考试成绩」「期末成绩」问的是**分数**不是
 *   考场安排，必须进成绩分支；纯考试问题（考试安排 / 什么时候考）
 *   不含成绩词，行为分毫不改；
 * - 独立成模块 + 纯函数，等 Agent 接入后**整文件删除**即可。
 */

/**
 * 命中关键词即视为成绩意图。
 *
 * 选取原则：覆盖「查分数」这件事的常见说法，但避开「学分」这类
 * 容易与选课问题混淆的词（「这门课几个学分」不是查成绩）。
 */
const SCORE_KEYWORDS = [
  '成绩',
  '分数',
  '绩点',
  '挂科',
  '多少分',
  '查分',
] as const

/**
 * 判断用户输入是否在问成绩。
 *
 * 纯函数：去掉所有空白后做包含匹配，与 `examIntent` / `courseIntent` 口径一致。
 */
export function isScoreQuery(text: string): boolean {
  const normalized = text.replace(/\s+/g, '')
  if (!normalized) return false

  return SCORE_KEYWORDS.some((keyword) => normalized.includes(keyword))
}

/**
 * 学期的后端格式：`YYYY-YYYY-N`（如 `2025-2026-1`）。
 *
 * 与 `ScoreController` / `data.sql` 种子逐字一致；后端做**精确匹配**且
 * **没有格式校验**（不存在的学期 = 200 + 空数组，不是错误），
 * 因此前端解析层的职责只有一个：把用户「明确写出的学期」
 * 变成这个格式，其它情况一律交还给调用方追问。
 */

/**
 * 写法一：直接就是后端格式——`2025-2026-1`。
 *
 * 「2026-2027-1」「2025-2026-1 学期的成绩」都能取到；
 * 三位学期号（`2025-2026-12`）不匹配（`(?!\d)` 挡住），当作未提供。
 */
const SEMESTER_LITERAL_PATTERN = /(\d{4})-(\d{4})-([12])(?!\d)/

/**
 * 写法二：中文写法——`2026-2027学年第一学期`、`2025-2026第1学期`、
 * `2025至2026学年第二学期`。年份分隔符允许 `-`、`—`、`–`、`至`、`到`、`~`、`/`，
 * 「学年」与「第」可省略，学期号接受 `一/二/1/2`。
 */
const SEMESTER_CHINESE_PATTERN =
  /(\d{4})\s*[-—–至到~/]?\s*(\d{4})\s*(?:学年\s*)?(?:第\s*)?([一二12])\s*学期/

/** 中文学期序号 → 后端数字序号 */
const SEMESTER_NUMBER: Record<string, string> = { 一: '1', 二: '2', '1': '1', '2': '2' }

/**
 * 从用户输入里抽取「明确写出的学期」。
 *
 * @returns 后端格式的学期串（如 `2025-2026-1`）；
 *          用户没有明确写出学期时返回 **undefined**
 *          （注意：不是空串、不是「当前学期」——前端没有依据就不猜，
 *          由调用方追问）。一句话里出现多个学期时只取第一个。
 *
 * 刻意**不做**的两件事：
 * - 不校验两个年份是否连续（`2025-2027-1` 照样放行）——后端精确匹配
 *   自然返回空列表，那是一个正确的「查无成绩」回答，不是错误；
 * - 不解析「这学期 / 上学期 / 下学期」等相对说法——没有学期日历，
 *   任何映射都是编造。
 */
export function parseSemester(text: string): string | undefined {
  const literal = text.match(SEMESTER_LITERAL_PATTERN)
  if (literal) {
    return `${literal[1]}-${literal[2]}-${literal[3]}`
  }

  const chinese = text.match(SEMESTER_CHINESE_PATTERN)
  if (chinese) {
    const semesterNumber = SEMESTER_NUMBER[chinese[3] ?? ''] ?? undefined
    if (semesterNumber === undefined) return undefined
    return `${chinese[1]}-${chinese[2]}-${semesterNumber}`
  }

  return undefined
}

/**
 * 判断用户是否**明确要求查全部学期**（「全部 / 所有」）。
 *
 * 只在成绩语境里有意义（调用方负责语境）：
 * 作为追问后的回答（「全部」「都要查」）或直接说
 * 「查我所有学期的成绩」时命中；命中即以 `semester: undefined`
 * 调用接口（后端语义：不按学期过滤，返回全部）。
 *
 * 注意它**不是**「没提供学期」的兜底——恰恰相反，它是一种
 * **明确的用户选择**，因此不需要追问。
 */
export function wantsAllSemesters(text: string): boolean {
  const normalized = text.replace(/\s+/g, '')
  if (!normalized) return false

  return normalized.includes('全部') || normalized.includes('所有')
}
