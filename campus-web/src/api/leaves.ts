/**
 * 请假 API —— 第四条真实后端链路（F3-4）。
 *
 * 契约来源（不猜字段）：后端 `campus-service`
 * - 路由：`LeaveApplicationController` → `@PostMapping` + `@RequestMapping("/api/leaves")`
 *   即真实地址 `POST /api/leaves`
 * - 请求体：`LeaveApplicationRequest` → studentId / startTime / endTime / reason（全 String）
 * - 响应：`LeaveApplicationResponse` → id / studentId / startTime / endTime / reason / status / createdAt
 * - **成功返回 201**（不是 200）
 * - 失败返回 400 + `{"message": "..."}`（中文可读原因）
 *
 * 请求方式与 scores / exams / courses 一致：原生 fetch + AbortController + Vite proxy。
 * 差异：这是 POST 请求（其它三个是 GET），需要发送 JSON body。
 */

import { API_BASE_URL, buildApiUrl, REQUEST_TIMEOUT_MS } from '@/config/env'
import type { LeaveApiErrorKind, LeaveQuery, LeaveResponse } from '@/types/leave'

/** 请假接口路径（与后端 `@RequestMapping` 逐字一致） */
export const LEAVES_PATH = '/api/leaves'

/** 归一化后的接口错误 */
export class LeaveApiError extends Error {
  readonly kind: LeaveApiErrorKind
  readonly status?: number
  /** 后端 400 返回的业务错误消息（已是中文） */
  readonly serverMessage?: string

  constructor(kind: LeaveApiErrorKind, message: string, status?: number, serverMessage?: string) {
    super(message)
    this.name = 'LeaveApiError'
    this.kind = kind
    this.status = status
    this.serverMessage = serverMessage
  }
}

const FAILURE_PREFIX = '请假提交失败'

/**
 * 校验响应体是否符合后端 DTO 结构。
 *
 * 严格校验：id 为 number，studentId/reason/status 为 string，
 * startTime/endTime/createdAt 为 string（ISO 8601）。
 */
function isLeaveResponse(value: unknown): value is LeaveResponse {
  if (typeof value !== 'object' || value === null) return false

  const record = value as Record<string, unknown>
  return (
    typeof record.id === 'number' &&
    typeof record.studentId === 'string' &&
    typeof record.startTime === 'string' &&
    typeof record.endTime === 'string' &&
    typeof record.reason === 'string' &&
    typeof record.status === 'string' &&
    typeof record.createdAt === 'string'
  )
}

function toNetworkError(error: unknown): LeaveApiError {
  const isAbort = error instanceof DOMException && error.name === 'AbortError'
  if (isAbort) {
    return new LeaveApiError('network', `${FAILURE_PREFIX}：请求超时，请确认后端服务已启动`)
  }

  return new LeaveApiError(
    'network',
    `${FAILURE_PREFIX}：无法连接后端服务，请确认服务已启动且地址正确`,
  )
}

/**
 * 提交一条请假申请。
 *
 * 成功：返回后端创建的申请记录（含 id、status=PENDING）。
 * 失败：抛 {@link LeaveApiError}，`message` 已是可直接展示的中文提示。
 *       400 错误时 `serverMessage` 携带后端返回的具体原因。
 */
export async function submitLeaveApplication(
  query: LeaveQuery,
  signal?: AbortSignal,
): Promise<LeaveResponse> {
  const url = buildApiUrl(LEAVES_PATH)

  const timeoutController = new AbortController()
  const timer = globalThis.setTimeout(() => timeoutController.abort(), REQUEST_TIMEOUT_MS)
  const abortExternally = () => timeoutController.abort()
  signal?.addEventListener('abort', abortExternally, { once: true })

  let response: Response
  try {
    response = await fetch(url, {
      method: 'POST',
      mode: API_BASE_URL ? 'cors' : 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify({
        studentId: query.studentId,
        startTime: query.startTime,
        endTime: query.endTime,
        reason: query.reason,
      }),
      signal: timeoutController.signal,
    })
  } catch (error) {
    throw toNetworkError(error)
  } finally {
    globalThis.clearTimeout(timer)
    signal?.removeEventListener('abort', abortExternally)
  }

  if (!response.ok) {
    let serverMessage: string | undefined
    try {
      const body = await response.json()
      if (typeof body === 'object' && body !== null && typeof (body as Record<string, unknown>).message === 'string') {
        serverMessage = (body as Record<string, string>).message
      }
    } catch {
      // 响应体不是 JSON，忽略
    }

    if (response.status === 400 && serverMessage) {
      throw new LeaveApiError(
        'http',
        `${FAILURE_PREFIX}：${serverMessage}`,
        response.status,
        serverMessage,
      )
    }

    throw new LeaveApiError(
      'http',
      `${FAILURE_PREFIX}：服务返回异常状态（HTTP ${response.status}）`,
      response.status,
    )
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new LeaveApiError('malformed', `${FAILURE_PREFIX}：返回内容不是合法 JSON`)
  }

  if (!isLeaveResponse(payload)) {
    throw new LeaveApiError('malformed', `${FAILURE_PREFIX}：返回数据字段与后端约定不一致`)
  }

  return payload
}

/**
 * 把提交成功的结果格式化为聊天区可展示的文本。
 */
export function formatLeaveResult(data: LeaveResponse): string {
  const lines = [
    '请假申请已提交成功！',
    '',
    `申请编号：${data.id}`,
    `学号：${data.studentId}`,
    `开始时间：${data.startTime}`,
    `结束时间：${data.endTime}`,
    `请假事由：${data.reason}`,
    `当前状态：${data.status}`,
    `提交时间：${data.createdAt}`,
  ]
  return lines.join('\n')
}
