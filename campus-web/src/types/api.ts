/**
 * 通用 API 契约类型。
 * 本阶段不定义任何业务实体，只保留后续接入后端时共用的外层结构。
 */

/** 后端统一响应包裹（接入 CampusFlow Backend 时确认后再启用） */
export interface ApiResponse<T = unknown> {
  code: number
  message: string
  data: T
}

/** 分页请求参数 */
export interface PageQuery {
  page: number
  size: number
}

/** 分页响应体 */
export interface PageResult<T> {
  list: T[]
  total: number
  page: number
  size: number
}
