/**
 * Axios 相关常量。
 * 本阶段不接后端、不写任何业务接口，仅提供基础实例所需参数。
 */

/** 默认请求超时（毫秒） */
export const API_TIMEOUT_MS = 30_000

/** 默认请求头 */
export const API_DEFAULT_HEADERS = {
  'Content-Type': 'application/json',
} as const
