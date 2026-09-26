/**
 * types 层统一出口。
 * 业务类型（课程、成绩、请假、Agent 消息等）后续按领域拆文件后在此 re-export。
 */
export type { ApiResponse, PageQuery, PageResult } from './api'
export type { ChatMessage, Conversation, MessageRole } from './conversation'
// 考试安排：字段逐字对照后端 DTO，见 `./exam` 顶部注释
export type { ExamApiErrorKind, ExamQuery, ExamSchedule } from './exam'
// 请假申请：字段逐字对照后端 DTO，见 `./leave` 顶部注释
export type {
  LeaveApiErrorKind,
  LeaveCollectedData,
  LeaveQuery,
  LeaveRequest,
  LeaveResponse,
} from './leave'
