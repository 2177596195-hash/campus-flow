/**
 * 请假（Leave）领域的前端类型。
 *
 * 契约来源：**不猜字段**，逐字对照后端 Spring Boot 源码：
 *
 * 请求 DTO：`LeaveApplicationRequest.java`
 * ```java
 * public record LeaveApplicationRequest(
 *     String studentId,   // 必填
 *     String startTime,   // 必填，形如 "2026-10-12 08:00"
 *     String endTime,     // 必填，不得早于 startTime
 *     String reason       // 必填，非空白，最多 512 字符
 * ) {}
 * ```
 *
 * 响应 DTO：`LeaveApplicationResponse.java`
 * ```java
 * public record LeaveApplicationResponse(
 *     Long id,
 *     String studentId,
 *     LocalDateTime startTime,   // ISO 8601: "2026-10-12T08:00:00"
 *     LocalDateTime endTime,
 *     String reason,
 *     String status,             // 初始为 "PENDING"
 *     LocalDateTime createdAt
 * ) {}
 * ```
 *
 * 关键契约细节：
 * 1. **成功返回 201**（不是 200）——这是本项目第一个「创建资源」的接口。
 * 2. **请求字段全部是 String**（包括时间），后端 Service 层统一解析。
 * 3. **时间接受两种格式**：`2026-10-12T08:00` 或 `2026-10-12 08:00`（空格会被归一化为 T），
 *    甚至只有日期 `2026-10-12`（按当天零点处理）。
 * 4. **400 响应体**形如 `{"message": "..."}`，message 是可读中文。
 * 5. **响应没有外层包裹**：直接是对象，不是 `{ code, data }` 形式。
 */

/** 请假提交请求（对应后端 `LeaveApplicationRequest`） */
export interface LeaveRequest {
  studentId: string
  startTime: string
  endTime: string
  reason: string
}

/** 请假提交响应（对应后端 `LeaveApplicationResponse`） */
export interface LeaveResponse {
  id: number
  studentId: string
  startTime: string
  endTime: string
  reason: string
  status: string
  createdAt: string
}

/** 请假提交接口的入参（与 LeaveRequest 相同，语义别名） */
export interface LeaveQuery {
  studentId: string
  startTime: string
  endTime: string
  reason: string
}

/** 请假流程中已收集到的信息 */
export interface LeaveCollectedData {
  startTime?: string
  endTime?: string
  reason?: string
}

/** 请求最终失败的原因分类 */
export type LeaveApiErrorKind =
  | 'network'
  | 'http'
  | 'malformed'
