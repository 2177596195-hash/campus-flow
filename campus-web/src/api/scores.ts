/**
 * 成绩 API —— 与 `@/api/exams` / `@/api/courses` 同款的第三条真实后端链路。
 *
 * 契约来源（不猜字段）：后端 `campus-service`
 * - 路由：`ScoreController` → `@GetMapping` + `@RequestMapping("/api/scores")`
 *   即真实地址 `GET /api/scores?studentId=...&semester=...`
 * - 响应：`ScoreResponse` record → semester / course 为字符串、
 *   score / credit 为**数值**的四字段裸数组（见 `@/types/score`）
 * - `semester` 是可选参数：不传返回该学生全部学期的成绩；
 *   传了做**精确匹配**（后端无格式校验，匹配不到 = 200 + `[]`）
 *
 * ── 为什么这里是**顶层模块**，而不是塞进 `@/api` barrel ──
 * 与考试 / 课程模块同一条理由：`@/api/index.ts` 是 **Axios 层**的门面，
 * 而本模块**不使用 Axios**。挂在同一个 barrel 上会出现
 * 「导入 API 方法却顺带拉起 Axios 实例」的隐式耦合。
 * 因此这里保持顶层、自成一体，只依赖 `@/config/env` 与 `@/types/score`。
 *
 * ── 请求方式与考试 / 课程模块完全一致 ──
 * 同源相对路径 `/api/scores` + Vite 开发服务器反代（见 `vite.config.ts`
 * 的 `server.proxy`），绕开后端无 CORS 配置的问题；仍用原生 fetch +
 * `AbortController`，因为需要「超时与外部 `AbortSignal` 合成一路取消」。
 * **对外签名（`fetchScoreRecords`）与错误类型保持不变**，
 * 将来若换实现，只需替换内部实现，调用方无需改动。
 *
 * ── 参数边界（本模块唯一的「额外」职责）──
 * `studentId` 与 `semester` 都**只从入参来**，不在模块内写死任何默认值：
 * 开发期的固定学生由 `@/config/env` 统一提供（`resolveStudentId()`），
 * 学期由调用方（意图层）显式传入或明确传 undefined（查全部学期）。
 * 拼查询串时 `semester` 只在「明确给出非空字符串」时才进 URL——
 * 后端虽然把空白学期当「未提供」处理（Service 归一化），但前端不依赖
 * 这个兜底：空值根本不该被发出去。
 */

import { API_BASE_URL, buildApiUrl, REQUEST_TIMEOUT_MS } from '@/config/env'
import type { ScoreApiErrorKind, ScoreQuery, ScoreRecord } from '@/types/score'

/** 成绩接口路径（与后端 `@RequestMapping` 逐字一致） */
export const SCORES_PATH = '/api/scores'

/** 归一化后的接口错误：调用方只需读 `message`（已是可直接展示的中文） */
export class ScoreApiError extends Error {
  readonly kind: ScoreApiErrorKind
  /** 有 HTTP 响应时的状态码，连不上时为 undefined */
  readonly status?: number

  constructor(kind: ScoreApiErrorKind, message: string, status?: number) {
    super(message)
    this.name = 'ScoreApiError'
    this.kind = kind
    this.status = status
  }
}

/** 失败时的统一前缀：页面上的「明确失败提示」由它保证 */
const FAILURE_PREFIX = '成绩查询失败'

/**
 * 校验一条记录是否**完全符合**后端 DTO：
 * 四字段；semester / course 为字符串，score / credit 为有限数值。
 *
 * 数值校验用 `typeof === 'number'` + `Number.isFinite`：
 * 后端 `BigDecimal` 经 Jackson 序列化为 JSON 数字，`JSON.parse` 得到 number；
 * 若后端某天把数值字符串化（或塞进 NaN / null），这里会立刻判 malformed，
 * 而不是让 `undefined` 悄悄溜进渲染层。
 */
function isScoreRecord(value: unknown): value is ScoreRecord {
  if (typeof value !== 'object' || value === null) return false

  const record = value as Record<string, unknown>
  return (
    typeof record.semester === 'string' &&
    typeof record.course === 'string' &&
    typeof record.score === 'number' &&
    Number.isFinite(record.score) &&
    typeof record.credit === 'number' &&
    Number.isFinite(record.credit)
  )
}

/**
 * 校验整个响应体：必须是数组，且每一项都符合 DTO。
 *
 * 刻意**严格**（宁可报错也不吞掉脏数据）：一旦后端改了字段名，
 * 这里会立刻变成明确的失败提示，而不是渲染出一片空白或 `undefined`，
 * 从而避免把「结构不对」误判成「这个学期没有成绩」。
 */
function parseScoreList(payload: unknown): ScoreRecord[] {
  if (!Array.isArray(payload)) {
    throw new ScoreApiError('malformed', `${FAILURE_PREFIX}：返回数据不是成绩列表`)
  }

  if (!payload.every(isScoreRecord)) {
    throw new ScoreApiError('malformed', `${FAILURE_PREFIX}：返回数据字段与后端约定不一致`)
  }

  return payload
}

/** 从 fetch 的失败里提取尽可能准确的原因（超时 / 连不上 / 被拦截） */
function toNetworkError(error: unknown): ScoreApiError {
  const isAbort = error instanceof DOMException && error.name === 'AbortError'
  if (isAbort) {
    return new ScoreApiError('network', `${FAILURE_PREFIX}：请求超时，请确认后端服务已启动`)
  }

  return new ScoreApiError(
    'network',
    `${FAILURE_PREFIX}：无法连接后端服务，请确认服务已启动且地址正确`,
  )
}

/**
 * 查询指定学生的成绩。
 *
 * 成功：返回数组；**空数组是正常结果**（该学生该学期没出分），
 *      由调用方渲染成「该学期暂无成绩」的正常回复，而不是失败提示。
 * 失败：抛 {@link ScoreApiError}，`message` 已是可直接展示的中文提示。
 *
 * @param query 学号 + 可选的学期（semester 为 undefined 时查全部学期）
 * @param signal 可选的中断信号（组件卸载 / 清空会话 / 重复发送时取消）
 */
export async function fetchScoreRecords(
  query: ScoreQuery,
  signal?: AbortSignal,
): Promise<ScoreRecord[]> {
  const params = new URLSearchParams({ studentId: query.studentId })
  // semester 只在「明确给出非空字符串」时才进 URL：undefined / 空串 / 空白
  // 都是「查全部学期」，不是「semester=undefined」这种脏参数；
  // 后端对空白学期的归一化是它自己的兜底，前端不依赖。
  if (typeof query.semester === 'string' && query.semester.trim().length > 0) {
    params.set('semester', query.semester)
  }
  const url = buildApiUrl(`${SCORES_PATH}?${params.toString()}`)

  // 超时与外部取消合成同一个信号：两条中断路径都只产生一次错误
  const timeoutController = new AbortController()
  const timer = globalThis.setTimeout(() => timeoutController.abort(), REQUEST_TIMEOUT_MS)
  const abortExternally = () => timeoutController.abort()
  signal?.addEventListener('abort', abortExternally, { once: true })

  let response: Response
  try {
    response = await fetch(url, {
      method: 'GET',
      // 相对路径即同源请求，跨源才需要 'cors'；由 mode 兜住两种部署形态
      // （开发期走代理 → same-origin；直连后端 → cors）
      mode: API_BASE_URL ? 'cors' : 'same-origin',
      headers: { Accept: 'application/json' },
      signal: timeoutController.signal,
    })
  } catch (error) {
    throw toNetworkError(error)
  } finally {
    globalThis.clearTimeout(timer)
    signal?.removeEventListener('abort', abortExternally)
  }

  if (!response.ok) {
    throw new ScoreApiError(
      'http',
      `${FAILURE_PREFIX}：服务返回异常状态（HTTP ${response.status}）`,
      response.status,
    )
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new ScoreApiError('malformed', `${FAILURE_PREFIX}：返回内容不是合法 JSON`)
  }

  return parseScoreList(payload)
}

/**
 * 把「渲染一屏聊天需要的文本」收敛在 API 层，组件不拼业务文案。
 *
 * `semester` 只是文案措辞的依据（「2025-2026-1 学期」/「全部学期」），
 * 数据过滤已经在后端完成，这里不做二次过滤；
 * 排序也由后端完成（学期从新到旧、同学期按课程名），这里按原样渲染。
 */
export function formatScoreRecords(
  studentId: string,
  scores: ScoreRecord[],
  semester?: string,
): string {
  if (scores.length === 0) {
    return semester === undefined
      ? `已查询学号 ${studentId} 的全部学期成绩：暂无任何成绩记录。`
      : `已查询学号 ${studentId} 的 ${semester} 学期成绩：该学期暂无成绩记录。`
  }

  const scope = semester === undefined ? '全部学期成绩' : `${semester} 学期成绩`
  const lines = scores.map(
    (record, index) =>
      `${index + 1}. ${record.semester}｜${record.course}｜成绩：${record.score}｜学分：${record.credit}`,
  )

  return [`已查询学号 ${studentId} 的${scope}，共 ${scores.length} 门：`, '', ...lines].join('\n')
}
