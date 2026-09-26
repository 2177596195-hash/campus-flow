/**
 * 本地占位（mock）助手回复。
 *
 * 本阶段**还没有接入 Agent**，为了让 UI 的「发送 → 出现回答」链路可以真实验证，
 * 这里用一份纯本地、纯函数的规则表生成回复。
 *
 * 边界（重要）：
 * - 不 import axios / api 层，不发任何网络请求，不 import 任何后端能力；
 * - 不承诺任何真实业务结论（成绩、课表等一律明确回答「尚未接入」），
 *   避免把假数据误当成真实查询结果；
 * - **考试安排已不再由本模块处理**：它已接通真实后端接口
 *   （见 `@/lib/examFlow` 与 `@/api/exams`），`ChatView` 在进入本模块之前就分流了。
 *   这里保留的考试规则只作为**兜底**（万一有调用方绕过分流直接调用本函数），
 *   文案明确说明「该问题由真实接口返回」，不再编造任何场次；
 * - 后续接入真实 Agent 时，本模块整体删除，替换成一次 API 调用即可。
 */

import type { ChatMessage } from '@/types'

/** 占位回复的固定前缀，用于在界面上一眼区分「这不是真实 Agent 输出」 */
const MOCK_TAG = '（本地占位回复，尚未接入 Agent）'

interface MockRule {
  /** 命中关键词 */
  keywords: string[]
  /** 回复正文 */
  reply: string
}

const RULES: MockRule[] = [
  {
    keywords: ['成绩', '分数', '绩点', '挂科'],
    reply: [
      '成绩查询是 CampusFlow 的第一批能力之一。',
      '',
      '当前前端处于 UI 骨架阶段，还没有连接后端与 Agent，因此无法返回真实成绩数据。',
      '接口接好之后，这里会直接给出你本学期的课程、学分与分数明细。',
    ].join('\n'),
  },
  {
    keywords: ['课表', '课程', '上课', '第几周'],
    reply: [
      '课表查询会按周次返回你的课程安排（课程名、教师、星期、节次、教室）。',
      '',
      '现在还没有接入后端，所以这里只是一条占位回复。',
      '接好之后你可以直接问「这周的课表」，Agent 会自动补齐周次参数。',
    ].join('\n'),
  },
  {
    keywords: ['考试', '考场', '补考', '期末'],
    reply: [
      '考试安排查询已经接通真实后端接口（GET /api/exams），这类问题不会再走本地占位回复。',
      '',
      '如果你是直接调用到这里的，说明绕过了 ChatView 的意图分流，请联系开发排查。',
    ].join('\n'),
  },
  {
    keywords: ['请假', '事假', '病假', '销假'],
    reply: [
      '请假是 CampusFlow 里唯一的「写操作」：提交后会生成一条申请记录并进入待审批状态。',
      '',
      '为了不产生任何真实数据，本阶段前端不会提交表单。',
      '接入后需要提供请假类型、起止时间和请假事由。',
    ].join('\n'),
  },
  {
    keywords: ['规则', '规定', '制度', '奖学金', '图书馆', '体育馆', '宿舍'],
    reply: [
      '校园规则类问题会走知识库检索（RAG），回答里会附带命中的资料出处。',
      '',
      '知识库检索同样还没接到前端，所以这里先给一条占位回复。',
      '接入后可以问「图书馆开放时间」这类问题。',
    ].join('\n'),
  },
]

const FALLBACK_REPLY = [
  '我收到了你的问题。',
  '',
  'CampusFlow 目前可以处理这几类校园事务：查询成绩、查询课表、查询考试安排、查询校园规则、办理请假。',
  '不过本阶段只完成了前端界面骨架，Agent 与后端接口尚未接入，所以还无法真正作答。',
  '',
  '你可以先点击左侧「新对话」体验会话切换，或试试欢迎页上的示例问题。',
].join('\n')

/**
 * 根据用户输入生成一条占位回复。
 *
 * 纯函数：同样的输入永远得到同样的输出，便于 UI 调试与后续替换。
 */
export function buildMockReply(userText: string): string {
  const normalized = userText.replace(/\s+/g, '')
  const matched = RULES.find((rule) =>
    rule.keywords.some((keyword) => normalized.includes(keyword)),
  )

  const body = matched ? matched.reply : FALLBACK_REPLY
  return `${MOCK_TAG}\n\n${body}`
}

/** 占位回复的模拟「思考」耗时，让发送后的状态切换可见（纯前端计时，无网络） */
export const MOCK_REPLY_DELAY_MS = 420

/** 构造占位回复消息（id / 时间戳由 store 负责，这里只提供内容与角色） */
export function buildMockAssistantMessage(userText: string): Pick<ChatMessage, 'role' | 'content' | 'mock'> {
  return { role: 'assistant', content: buildMockReply(userText), mock: true }
}
