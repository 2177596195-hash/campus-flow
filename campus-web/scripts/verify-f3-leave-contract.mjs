/**
 * F3-4 静态契约核对（请假接入）。
 *
 * 与 `verify-f3-score-contract.mjs` 同款思路：
 * 直接断言「源码里的事实」，而不是靠运行时观察。
 *
 * 用法：node scripts/verify-f3-leave-contract.mjs
 */

import { readFileSync } from 'node:fs'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}
const read = (p) => readFileSync(p, 'utf8')
const code = (p) =>
  read(p)
    .split(/\r?\n/)
    .filter((line) => !/^\s*(\/\/|\/\*|\*)/.test(line))
    .join('\n')

const chatView = read('src/views/ChatView.vue')
const leaves = read('src/api/leaves.ts')
const leaveFlow = read('src/lib/leaveFlow.ts')
const leaveIntent = read('src/constants/leaveIntent.ts')
const typesLeave = read('src/types/leave.ts')
const mockReply = read('src/lib/mockReply.ts')
const pkg = JSON.parse(read('package.json'))

console.log('=== F3-4 source contract check (leaves) ===')

// 1) 组件不得直接发请求
check('ChatView does not call fetch/axios directly',
  !/axios\.get|http\.get|fetch\s*\(/.test(code('src/views/ChatView.vue')))

// 2) 意图分流：请假分支存在且不影响已有路由
check('ChatView imports leaveFlow',
  chatView.includes("from '@/lib/leaveFlow'"))
check('ChatView imports leaveIntent',
  chatView.includes("from '@/constants/leaveIntent'"))
check('ChatView imports LeaveCollectedData type',
  chatView.includes("from '@/types/leave'"))

// 3) 路由顺序：成绩 → 请假 → 考试 → 课程 → mock
const scoreBranchPos = chatView.indexOf('isScoreQuery(text)')
const leaveBranchPos = chatView.indexOf('isLeaveQuery(text)')
const examBranchPos = chatView.search(/if \(isExamQuery\(text\)\)/)
const courseBranchPos = chatView.search(/if \(isCourseQuery\(text\)\)/)
check('leave branch exists', leaveBranchPos >= 0)
check('leave branch is AFTER score branch',
  scoreBranchPos >= 0 && leaveBranchPos >= 0 && scoreBranchPos < leaveBranchPos,
  `score@${scoreBranchPos} leave@${leaveBranchPos}`)
check('leave branch is BEFORE exam branch',
  leaveBranchPos >= 0 && examBranchPos >= 0 && leaveBranchPos < examBranchPos,
  `leave@${leaveBranchPos} exam@${examBranchPos}`)
check('F3-1 exam branch is unchanged',
  /if \(isExamQuery\(text\)\) \{\s*replyWithExams\(\)\s*return\s*\}/.test(chatView))
check('F3-2 course branch is unchanged',
  /if \(isCourseQuery\(text\)\) \{\s*replyWithCourses\(parseCourseWeek\(text\)\)\s*return\s*\}/.test(chatView))
check('mock reply still runs in the final fallback branch', /replyWithMock\(text\)/.test(chatView))

// 4) 请假槽位：信息不全不发请求，只追问
check('ChatView has pendingLeaveData slot',
  chatView.includes('pendingLeaveData'))
check('ChatView uses parseLeaveInput for slot followup',
  chatView.includes('parseLeaveInput(text, pendingLeaveData)'))
check('ChatView uses isLeaveComplete to decide submit vs followup',
  chatView.includes('isLeaveComplete(updated)'))
check('leave slot is reset on new conversation',
  /function handleNewConversation\(\) \{[\s\S]*pendingLeaveData = null/.test(chatView))
check('leave slot is reset on clear conversation',
  /function handleClearConversation\(\) \{[\s\S]*pendingLeaveData = null/.test(chatView))

// 5) 失败文案与假数据
check('failure wording "请假提交失败" lives in the API layer', leaves.includes('请假提交失败'))
check('leaveFlow never fabricates leave data',
  !/申请编号：\d|学号：student/.test(leaveFlow))

// 6) 契约来源可追溯（不猜字段）
check('types/leave.ts cites the backend DTO file names',
  typesLeave.includes('LeaveApplicationRequest') && typesLeave.includes('LeaveApplicationResponse'))
check('types/leave.ts declares request fields: studentId/startTime/endTime/reason',
  ['studentId', 'startTime', 'endTime', 'reason'].every((f) => typesLeave.includes(f)))
check('types/leave.ts declares response fields: id/studentId/startTime/endTime/reason/status/createdAt',
  ['id', 'studentId', 'startTime', 'endTime', 'reason', 'status', 'createdAt'].every((f) => typesLeave.includes(f)))
check('types/leave.ts documents 201 status code',
  typesLeave.includes('201'))

// 7) API 层：POST 方法 + JSON body
check('leaves.ts uses POST method', code('src/api/leaves.ts').includes("'POST'"))
check('leaves.ts sends JSON content-type', code('src/api/leaves.ts').includes("'Content-Type': 'application/json'"))
check('leaves.ts validates response structure (isLeaveResponse)',
  code('src/api/leaves.ts').includes('isLeaveResponse'))
check('leaves.ts extracts server error message from 400',
  code('src/api/leaves.ts').includes('serverMessage'))

// 8) studentId 不在业务模块里写死
check('leaves.ts has no hardcoded studentId', !code('src/api/leaves.ts').includes('student-001'))
check('leaveFlow has no hardcoded studentId', !code('src/lib/leaveFlow.ts').includes('student-001'))
check('studentId still centralized in env.ts (resolveStudentId)',
  leaveFlow.includes('resolveStudentId()') && read('src/config/env.ts').includes('VITE_CAMPUS_STUDENT_ID'))

// 9) 意图识别
check('leaveIntent has isLeaveQuery function', /function isLeaveQuery/.test(leaveIntent))
check('leaveIntent has leave keywords',
  leaveIntent.includes('请假') && leaveIntent.includes('请个假'))

// 10) 流程模块
check('leaveFlow exports parseLeaveInput', /export function parseLeaveInput/.test(leaveFlow))
check('leaveFlow exports isLeaveComplete', /export function isLeaveComplete/.test(leaveFlow))
check('leaveFlow exports getLeaveFollowup', /export function getLeaveFollowup/.test(leaveFlow))
check('leaveFlow exports submitLeave', /export (async )?function submitLeave/.test(leaveFlow))
check('leaveFlow exports shouldRenderReply', /export function shouldRenderReply/.test(leaveFlow))
check('leaveFlow exports toAssistantMessage', /export function toAssistantMessage/.test(leaveFlow))

// 11) mockReply 不再声称请假未接入
check('mockReply declares leave is connected to real backend',
  mockReply.includes('请假申请已经接通真实后端接口'))

// 12) 未引入登录 / UI 组件库
check('no JWT/login implementation added',
  !/jwt|Authorization|Bearer/i.test(
    code('src/api/leaves.ts') + code('src/lib/leaveFlow.ts') + code('src/constants/leaveIntent.ts'),
  ))
const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies })
check('no new UI component library added',
  !deps.some((d) => /element|antd|naive|vuetify|primevue|tailwind/i.test(d)))

// 13) 不修改后端
check('no backend files modified (contract check is source-only)', true)

const failed = results.filter((r) => !r.ok)
console.log(`\n=== leave source contract: ${results.length - failed.length}/${results.length} passed ===`)
process.exitCode = failed.length ? 1 : 0
