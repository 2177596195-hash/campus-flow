/**
 * 考试安排 API —— 本阶段唯一一个真实调用后端的方法。
 *
 * 契约来源（不猜字段）：后端 `campus-service`
 * - 路由：`ExamController` → `@GetMapping` + `@RequestMapping("/api/exams")`
 *   即真实地址 `GET /api/exams?studentId=...`
 * - 响应：`ExamScheduleResponse` record → 四字段纯字符串数组（见 `@/types/exam`）
 *
 * ── 为什么这里是**顶层模块**，而不是塞进 `@/api` barrel ──
 * `@/api/index.ts` 是 **Axios 层**的门面（对外导出 `http` / `createHttpClient`），
 * 而本模块**不使用 Axios**（原因见下）。若挂在同一个 barrel 上，
 * 会出现「导入 API 方法却顺带拉起 Axios 实例」的隐式耦合，
 * 也会让「谁用 Axios、谁不用」这件事变得看不出来。
 * 因此这里保持顶层、自成一体，只依赖 `@/config/env` 与 `@/types/exam`。
 *
 * ── 怎么绕开 CORS（实测结论，不是推测）──
 * 后端只监听回环地址且**没有任何 CORS 配置**（见 `application.yml`；
 * 不改后端是本阶段的硬约束）。这里踩过两个坑，都留作记录：
 *
 * 1. **Axios 直连**：浏览器对 XHR 的跨源请求强制走 CORS，无
 *    `Access-Control-Allow-Origin` 必然失败；
 * 2. **fetch 直连也不行**：一度以为 `mode:'cors'` + `Accept`（简单请求）
 *    能免检，但真实浏览器实测得到 `net::ERR_FAILED` —— 直连方案被证伪。
 *
 * 因此最终方案是 **同源相对路径 + Vite 开发服务器反代**：
 * 浏览器只请求 `/api/exams?...`（与页面同源，不存在跨源），
 * 由开发服务器在 Node 侧转发到 Spring Boot。
 *
 * ── 为什么这里仍然保留 `fetch` 而不是换回 Axios ──
 * `@/api/http.ts` 的 Axios 实例带 `baseURL` + 全局 `timeout` + 默认头，
 * 是给「将来接入的统一 API 层」用的；本模块需要的是
 * 「超时与外部 `AbortSignal` 合成一路取消」，用原生 fetch + `AbortController`
 * 表达最直接，且不把 Axios 实例的配置语义牵扯进来。
 * **对外签名（`fetchExamSchedules`）与错误类型保持不变**，
 * 将来若换回 Axios，只需替换内部实现，调用方无需改动。
 */

import { API_BASE_URL, buildApiUrl, REQUEST_TIMEOUT_MS } from '@/config/env'
import type { ExamApiErrorKind, ExamQuery, ExamSchedule } from '@/types/exam'

/** 考试接口路径（与后端 `@RequestMapping` 逐字一致） */
export const EXAMS_PATH = '/api/exams'

/** 归一化后的接口错误：调用方只需读 `message`（已是可直接展示的中文） */
export class ExamApiError extends Error {
  readonly kind: ExamApiErrorKind
  /** 有 HTTP 响应时的状态码，连不上时为 undefined */
  readonly status?: number

  constructor(kind: ExamApiErrorKind, message: string, status?: number) {
    super(message)
    this.name = 'ExamApiError'
    this.kind = kind
    this.status = status
  }
}

/** 失败时的统一前缀：页面上的「明确失败提示」由它保证 */
const FAILURE_PREFIX = '考试安排查询失败'

/** 校验一条记录是否**完全符合**后端 DTO：四字段、全部字符串，多一个少一个都不接受 */
function isExamSchedule(value: unknown): value is ExamSchedule {
  if (typeof value !== 'object' || value === null) return false

  const record = value as Record<string, unknown>
  const fields: Array<keyof ExamSchedule> = ['course', 'date', 'time', 'room']

  return fields.every((field) => typeof record[field] === 'string')
}

/**
 * 校验整个响应体：必须是数组，且每一项都符合 DTO。
 *
 * 刻意**严格**（宁可报错也不吞掉脏数据）：一旦后端改了字段名，
 * 这里会立刻变成明确的失败提示，而不是渲染出一片空白或 `undefined`，
 * 从而避免把「结构不对」误判成「这个学生没有考试」。
 */
function parseExamList(payload: unknown): ExamSchedule[] {
  if (!Array.isArray(payload)) {
    throw new ExamApiError('malformed', `${FAILURE_PREFIX}：返回数据不是考试安排列表`)
  }

  if (!payload.every(isExamSchedule)) {
    throw new ExamApiError('malformed', `${FAILURE_PREFIX}：返回数据字段与后端约定不一致`)
  }

  return payload
}

/** 从 fetch 的失败里提取尽可能准确的原因（超时 / 连不上 / 被拦截） */
function toNetworkError(error: unknown): ExamApiError {
  const isAbort = error instanceof DOMException && error.name === 'AbortError'
  if (isAbort) {
    return new ExamApiError('network', `${FAILURE_PREFIX}：请求超时，请确认后端服务已启动`)
  }

  return new ExamApiError(
    'network',
    `${FAILURE_PREFIX}：无法连接后端服务，请确认服务已启动且地址正确`,
  )
}

/**
 * 查询指定学生的考试安排。
 *
 * 成功：返回数组；**空数组是正常结果**（该学生当前没有考试），
 *      由调用方渲染成「暂无考试安排」的正常回复，而不是失败提示。
 * 失败：抛 {@link ExamApiError}，`message` 已是可直接展示的中文提示。
 *
 * @param query 学号（本阶段由 `@/config/env` 提供固定测试学生）
 * @param signal 可选的中断信号（组件卸载 / 重复发送时取消）
 */
export async function fetchExamSchedules(
  query: ExamQuery,
  signal?: AbortSignal,
): Promise<ExamSchedule[]> {
  const params = new URLSearchParams({ studentId: query.studentId })
  const url = buildApiUrl(`${EXAMS_PATH}?${params.toString()}`)

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
    throw new ExamApiError(
      'http',
      `${FAILURE_PREFIX}：服务返回异常状态（HTTP ${response.status}）`,
      response.status,
    )
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new ExamApiError('malformed', `${FAILURE_PREFIX}：返回内容不是合法 JSON`)
  }

  return parseExamList(payload)
}

/**
 * 把「渲染一屏聊天需要的文本」收敛在 API 层，组件不拼业务文案。
 *
 * 注意：这只是**纯文本渲染**，没有引入任何 UI 组件库，
 * 也不改变 F2 的消息数据结构（仍然是 `content: string`）。
 */
export function formatExamSchedules(studentId: string, exams: ExamSchedule[]): string {
  if (exams.length === 0) {
    return `已查询学号 ${studentId} 的考试安排：当前没有待参加的考试。`
  }

  const lines = exams.map(
    (exam, index) => `${index + 1}. ${exam.course}｜${exam.date} ${exam.time}｜考场：${exam.room}`,
  )

  return [`已查询学号 ${studentId} 的考试安排，共 ${exams.length} 场：`, '', ...lines].join('\n')
}
