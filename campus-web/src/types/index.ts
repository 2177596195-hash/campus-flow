/**
 * types 层统一出口。
 * 业务类型（课程、成绩、请假、Agent 消息等）后续按领域拆文件后在此 re-export。
 */
export type { ApiResponse, PageQuery, PageResult } from './api'
export type { ChatMessage, Conversation, MessageRole } from './conversation'
