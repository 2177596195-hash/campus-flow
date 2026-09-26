/**
 * 课程安排 API —— 与 `@/api/exams` 同款的第二条真实后端链路。
 *
 * 契约来源（不猜字段）：后端 `campus-service`
 * - 路由：`CourseController` → `@GetMapping` + `@RequestMapping("/api/courses")`
 *   即真实地址 `GET /api/courses?studentId=...&week=...`
 * - 响应：`CourseScheduleResponse` record → 六字段纯字符串数组（见 `@/types/course`）
 * - `week` 是可选参数：不传返回整学期课表；传了只返回「覆盖该周」的课程
 *
 * ── 为什么这里是**顶层模块**，而不是塞进 `@/api` barrel ──
 * 与考试模块同一条理由：`@/api/index.ts` 是 **Axios 层**的门面，
 * 而本模块**不使用 Axios**（原因见下）。挂在同一个 barrel 上会出现
 * 「导入 API 方法却顺带拉起 Axios 实例」的隐式耦合。
 * 因此这里保持顶层、自成一体，只依赖 `@/config/env` 与 `@/types/course`。
 *
 * ── 请求方式与考试模块完全一致 ──
 * 同源相对路径 `/api/courses` + Vite 开发服务器反代（见 `vite.config.ts`
 * 的 `server.proxy`），绕开后端无 CORS 配置的问题；仍用原生 fetch +
 * `AbortController`，因为需要「超时与外部 `AbortSignal` 合成一路取消」。
 * **对外签名（`fetchCourseSchedules`）与错误类型保持不变**，
 * 将来若换实现，只需替换内部实现，调用方无需改动。
 *
 * ── 参数边界（本模块唯一的「额外」职责）──
 * `studentId` 与 `week` 都**只从入参来**，不在模块内写死任何默认值：
 * 开发期的固定学生由 `@/config/env` 统一提供（`resolveStudentId()`），
 * 周次由调用方（意图层）显式传入或明确传 undefined（整学期）。
 * 拼查询串时 `week` 只在「明确给出数字」时才进 URL——
 * 否则会把 `undefined` 序列化成字符串挂到请求上，后端按非法参数返回 400。
 */

import { API_BASE_URL, buildApiUrl, REQUEST_TIMEOUT_MS } from '@/config/env'
import type { CourseApiErrorKind, CourseQuery, CourseSchedule } from '@/types/course'

/** 课程接口路径（与后端 `@RequestMapping` 逐字一致） */
export const COURSES_PATH = '/api/courses'

/** 归一化后的接口错误：调用方只需读 `message`（已是可直接展示的中文） */
export class CourseApiError extends Error {
  readonly kind: CourseApiErrorKind
  /** 有 HTTP 响应时的状态码，连不上时为 undefined */
  readonly status?: number

  constructor(kind: CourseApiErrorKind, message: string, status?: number) {
    super(message)
    this.name = 'CourseApiError'
    this.kind = kind
    this.status = status
  }
}

/** 失败时的统一前缀：页面上的「明确失败提示」由它保证 */
const FAILURE_PREFIX = '课表查询失败'

/** 校验一条记录是否**完全符合**后端 DTO：六字段、全部字符串，多一个少一个都不接受 */
function isCourseSchedule(value: unknown): value is CourseSchedule {
  if (typeof value !== 'object' || value === null) return false

  const record = value as Record<string, unknown>
  const fields: Array<keyof CourseSchedule> = [
    'course',
    'teacher',
    'weekday',
    'time',
    'room',
    'weeks',
  ]

  return fields.every((field) => typeof record[field] === 'string')
}

/**
 * 校验整个响应体：必须是数组，且每一项都符合 DTO。
 *
 * 刻意**严格**（宁可报错也不吞掉脏数据）：一旦后端改了字段名，
 * 这里会立刻变成明确的失败提示，而不是渲染出一片空白或 `undefined`，
 * 从而避免把「结构不对」误判成「这个学生没有课」。
 */
function parseCourseList(payload: unknown): CourseSchedule[] {
  if (!Array.isArray(payload)) {
    throw new CourseApiError('malformed', `${FAILURE_PREFIX}：返回数据不是课程安排列表`)
  }

  if (!payload.every(isCourseSchedule)) {
    throw new CourseApiError('malformed', `${FAILURE_PREFIX}：返回数据字段与后端约定不一致`)
  }

  return payload
}

/** 从 fetch 的失败里提取尽可能准确的原因（超时 / 连不上 / 被拦截） */
function toNetworkError(error: unknown): CourseApiError {
  const isAbort = error instanceof DOMException && error.name === 'AbortError'
  if (isAbort) {
    return new CourseApiError('network', `${FAILURE_PREFIX}：请求超时，请确认后端服务已启动`)
  }

  return new CourseApiError(
    'network',
    `${FAILURE_PREFIX}：无法连接后端服务，请确认服务已启动且地址正确`,
  )
}

/**
 * 查询指定学生的课程安排。
 *
 * 成功：返回数组；**空数组是正常结果**（该学生该周没有课），
 *      由调用方渲染成「第 N 周没有课程」的正常回复，而不是失败提示。
 * 失败：抛 {@link CourseApiError}，`message` 已是可直接展示的中文提示。
 *
 * @param query 学号 + 可选的教学周（week 为 undefined 时查整学期）
 * @param signal 可选的中断信号（组件卸载 / 重复发送时取消）
 */
export async function fetchCourseSchedules(
  query: CourseQuery,
  signal?: AbortSignal,
): Promise<CourseSchedule[]> {
  const params = new URLSearchParams({ studentId: query.studentId })
  // week 只在「明确给出」时才进 URL：undefined / null 都是「整学期」，
  // 不是「第 0 周」也不是「week=undefined」这种脏参数
  if (typeof query.week === 'number' && Number.isFinite(query.week)) {
    params.set('week', String(query.week))
  }
  const url = buildApiUrl(`${COURSES_PATH}?${params.toString()}`)

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
    throw new CourseApiError(
      'http',
      `${FAILURE_PREFIX}：服务返回异常状态（HTTP ${response.status}）`,
      response.status,
    )
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new CourseApiError('malformed', `${FAILURE_PREFIX}：返回内容不是合法 JSON`)
  }

  return parseCourseList(payload)
}

/**
 * 把「渲染一屏聊天需要的文本」收敛在 API 层，组件不拼业务文案。
 *
 * `week` 只是文案措辞的依据（「第 3 周」/「整学期」），
 * 数据过滤已经在后端完成，这里不做二次过滤。
 */
export function formatCourseSchedules(
  studentId: string,
  courses: CourseSchedule[],
  week?: number,
): string {
  if (courses.length === 0) {
    return week === undefined
      ? `已查询学号 ${studentId} 的整学期课程安排：本学期暂无课程记录。`
      : `已查询学号 ${studentId} 的第 ${week} 周课程安排：这一周没有课程。`
  }

  const scope = week === undefined ? '整学期课程安排（全部周次）' : `第 ${week} 周课程安排`
  const lines = courses.map(
    (course, index) =>
      `${index + 1}. ${course.course}｜${course.weekday} ${course.time}｜教室：${course.room}｜${course.teacher}｜${course.weeks}`,
  )

  return [`已查询学号 ${studentId} 的${scope}，共 ${courses.length} 门：`, '', ...lines].join('\n')
}
