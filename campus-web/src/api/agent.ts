/**
 * Agent 对话 API —— 前端与 Python Agent 服务之间的 HTTP 通道。
 *
 * 调用链（开发期）：
 *   Vue → Vite proxy `/api/agent` → Python Agent (port 8000)
 *     → LangGraph → Score Tool → Spring Boot `/api/scores` → MySQL
 *     → Agent 生成自然语言 → 原路返回 Vue
 *
 * 契约：
 *   POST /api/agent/chat
 *   请求：{ message: string, threadId?: string }
 *   响应：{ reply: string, intent: string, toolCalls: string[], threadId: string }
 *
 * 为什么独立模块（不放进 `@/api/index.ts`）：
 *   与 `@/api/scores.ts` 同款理由——本模块**不使用 Axios**，
 *   用原生 fetch + AbortController，挂在 Axios barrel 上会引入隐式耦合。
 *
 * F3 的 `@/api/scores.ts` 保留不动，作为 F3 直连 Spring Boot 的备用链路。
 */

import { API_BASE_URL, buildApiUrl, REQUEST_TIMEOUT_MS } from '@/config/env'

/** Agent 对话接口路径 */
export const AGENT_CHAT_PATH = '/api/agent/chat'

/** Agent 健康检查路径 */
export const AGENT_HEALTH_PATH = '/api/agent/health'

/** Agent 接口错误的分类 */
export type AgentErrorKind = 'network' | 'http' | 'malformed' | 'timeout'

/** Agent 对话响应 */
export interface AgentChatResult {
  reply: string
  intent: string
  toolCalls: string[]
  threadId: string
}

/** Agent 接口错误：`message` 已是可直接展示的中文 */
export class AgentApiError extends Error {
  readonly kind: AgentErrorKind
  readonly status?: number

  constructor(kind: AgentErrorKind, message: string, status?: number) {
    super(message)
    this.name = 'AgentApiError'
    this.kind = kind
    this.status = status
  }
}

const FAILURE_PREFIX = '智能助手暂时无法回答'

/**
 * 发起一轮 Agent 对话。
 *
 * @param message 用户本轮输入
 * @param threadId 会话标识（多轮时传入上一轮返回的 threadId）；缺失时 Agent 自动生成
 * @param signal 可选的中断信号
 *
 * 成功：返回 AgentChatResult（reply 是自然语言回答）
 * 失败：抛 AgentApiError，message 已是可直接展示的中文提示
 */
export async function sendAgentChat(
  message: string,
  threadId?: string,
  signal?: AbortSignal,
): Promise<AgentChatResult> {
  const url = buildApiUrl(AGENT_CHAT_PATH)

  const timeoutController = new AbortController()
  const timer = globalThis.setTimeout(() => timeoutController.abort(), REQUEST_TIMEOUT_MS)
  const abortExternally = () => timeoutController.abort()
  signal?.addEventListener('abort', abortExternally, { once: true })

  let response: Response
  try {
    response = await fetch(url, {
      method: 'POST',
      mode: API_BASE_URL ? 'cors' : 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ message, threadId: threadId || undefined }),
      signal: timeoutController.signal,
    })
  } catch (error) {
    const isAbort = error instanceof DOMException && error.name === 'AbortError'
    throw new AgentApiError(
      isAbort ? 'timeout' : 'network',
      isAbort
        ? `${FAILURE_PREFIX}：请求超时，请确认 Agent 服务已启动`
        : `${FAILURE_PREFIX}：无法连接 Agent 服务，请确认服务已启动`,
    )
  } finally {
    globalThis.clearTimeout(timer)
    signal?.removeEventListener('abort', abortExternally)
  }

  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : ''
    } catch {
      // 响应体不是 JSON，用状态码兜底
    }
    throw new AgentApiError(
      'http',
      detail || `${FAILURE_PREFIX}：服务返回异常（HTTP ${response.status}）`,
      response.status,
    )
  }

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new AgentApiError('malformed', `${FAILURE_PREFIX}：返回内容不是合法 JSON`)
  }

  if (!isAgentChatResult(payload)) {
    throw new AgentApiError('malformed', `${FAILURE_PREFIX}：返回数据结构与约定不一致`)
  }

  return payload
}

/** 校验响应体是否符合 AgentChatResult 契约 */
function isAgentChatResult(value: unknown): value is AgentChatResult {
  if (typeof value !== 'object' || value === null) return false
  const obj = value as Record<string, unknown>
  return (
    typeof obj.reply === 'string' &&
    typeof obj.intent === 'string' &&
    Array.isArray(obj.toolCalls) &&
    typeof obj.threadId === 'string'
  )
}

/**
 * 检查 Agent 服务是否可用。
 *
 * 用于页面加载时预检：不可用时可提前给出提示，而不是等用户发了消息才失败。
 * 失败时返回 false（不抛异常），调用方据此决定是否降级。
 */
export async function checkAgentHealth(signal?: AbortSignal): Promise<boolean> {
  const url = buildApiUrl(AGENT_HEALTH_PATH)
  try {
    const response = await fetch(url, {
      method: 'GET',
      mode: API_BASE_URL ? 'cors' : 'same-origin',
      headers: { Accept: 'application/json' },
      signal,
    })
    if (!response.ok) return false
    const body = await response.json()
    return typeof body === 'object' && body !== null && (body as Record<string, unknown>).status === 'ok'
  } catch {
    return false
  }
}
