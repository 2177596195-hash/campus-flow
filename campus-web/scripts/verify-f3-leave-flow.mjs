/**
 * F3-4 请假链路验证（真实模块 + 真实 HTTP，无浏览器）。
 *
 * 验证：
 *   1. 请假意图识别（含与成绩 / 考试 / 课程意图的边界）；
 *   2. 日期解析：ISO 格式 / 中文格式 / 相对日期 / 带时刻 / 不带时刻；
 *   3. 事由提取：有指示词 / 无指示词 / 纯意图短语（无事由）；
 *   4. 多轮追问：缺 startTime / 缺 endTime / 缺 reason / 全部收集完；
 *   5. 直接调 API：成功提交（201）/ 缺字段（400）/ 时间倒置（400）；
 *   6. 编排层：成功文案含申请编号、绝不编造数据；
 *   7. F3-1/F3-2/F3-3 回归：已有意图识别和流程不受影响。
 *
 * 用法：node scripts/verify-f3-leave-flow.mjs
 */

import { createServer } from 'vite'

const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080'
const STUDENT_ID = process.env.STUDENT_ID ?? 'student-001'

process.env.VITE_API_BASE_URL = BASE_URL
process.env.VITE_CAMPUS_STUDENT_ID = STUDENT_ID

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}

const srcRoot = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

const server = await createServer({
  root: process.cwd(),
  logLevel: 'error',
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true },
  resolve: { alias: [{ find: '@', replacement: srcRoot }] },
})

try {
  const env = await server.ssrLoadModule('/src/config/env.ts')
  const leaveIntent = await server.ssrLoadModule('/src/constants/leaveIntent.ts')
  const scoreIntent = await server.ssrLoadModule('/src/constants/scoreIntent.ts')
  const examIntent = await server.ssrLoadModule('/src/constants/examIntent.ts')
  const courseIntent = await server.ssrLoadModule('/src/constants/courseIntent.ts')
  const leavesApi = await server.ssrLoadModule('/src/api/leaves.ts')
  const flow = await server.ssrLoadModule('/src/lib/leaveFlow.ts')

  console.log('=== F3-4 leave query chain verification ===')
  console.log(`BaseURL=${env.API_BASE_URL} StudentId=${env.resolveStudentId()} Path=${leavesApi.LEAVES_PATH}`)

  // ---------- 1) 意图路由 ----------
  console.log('\n--- 1) intent routing ---')
  const leaveQuestions = [
    '我要请假',
    '帮我请个假',
    '我要申请请假',
    '提交请假',
    '我想请事假',
    '请病假',
    '我要休假',
  ]
  leaveQuestions.forEach((q) => check(`leave question hits leave intent: ${q}`, leaveIntent.isLeaveQuery(q)))

  check('score question does NOT hit leave intent', !leaveIntent.isLeaveQuery('查一下我的成绩'))
  check('exam question does NOT hit leave intent', !leaveIntent.isLeaveQuery('帮我查一下考试安排'))
  check('course question does NOT hit leave intent', !leaveIntent.isLeaveQuery('查看第3周课程'))
  check('leave intent regression: score intent still works', scoreIntent.isScoreQuery('查一下我的成绩'))
  check('leave intent regression: exam intent still works', examIntent.isExamQuery('帮我查一下考试安排'))
  check('leave intent regression: course intent still works', courseIntent.isCourseQuery('查看第3周课程'))

  // ---------- 2) 日期解析 ----------
  console.log('\n--- 2) date parsing ---')
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const tomorrow = new Date(today)
  tomorrow.setDate(tomorrow.getDate() + 1)
  const dayAfter = new Date(today)
  dayAfter.setDate(dayAfter.getDate() + 2)

  // ISO 格式
  let parsed = flow.parseLeaveInput('2026-10-12T08:00', {})
  check('ISO datetime: 2026-10-12T08:00', parsed.startTime === '2026-10-12 08:00', parsed.startTime)

  parsed = flow.parseLeaveInput('2026-10-12 08:00', {})
  check('ISO datetime with space: 2026-10-12 08:00', parsed.startTime === '2026-10-12 08:00', parsed.startTime)

  parsed = flow.parseLeaveInput('2026-10-12', {})
  check('ISO date only: 2026-10-12', parsed.startTime === '2026-10-12 00:00', parsed.startTime)

  // 中文格式
  parsed = flow.parseLeaveInput('10月12日 8:00', {})
  check('Chinese datetime: 10月12日 8:00', parsed.startTime === '2026-10-12 08:00', parsed.startTime)

  parsed = flow.parseLeaveInput('10月12日', {})
  check('Chinese date only: 10月12日',
    parsed.startTime?.startsWith('2026-10-12') === true, parsed.startTime)

  // 相对日期
  const tomorrowStr = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, '0')}-${String(tomorrow.getDate()).padStart(2, '0')}`
  const dayAfterStr = `${dayAfter.getFullYear()}-${String(dayAfter.getMonth() + 1).padStart(2, '0')}-${String(dayAfter.getDate()).padStart(2, '0')}`

  parsed = flow.parseLeaveInput('明天', {})
  check('relative date: 明天', parsed.startTime?.startsWith(tomorrowStr) === true, parsed.startTime)

  parsed = flow.parseLeaveInput('后天', {})
  check('relative date: 后天', parsed.startTime?.startsWith(dayAfterStr) === true, parsed.startTime)

  // 两个日期
  parsed = flow.parseLeaveInput('10月12日到10月14日', {})
  check('two dates: startTime = first date', parsed.startTime === '2026-10-12 00:00', parsed.startTime)
  check('two dates: endTime = second date', parsed.endTime === '2026-10-14 00:00', parsed.endTime)

  // 增量解析
  parsed = flow.parseLeaveInput('10月14日', { startTime: '2026-10-12 08:00' })
  check('incremental: endTime filled when startTime exists',
    parsed.startTime === '2026-10-12 08:00' && parsed.endTime === '2026-10-14 00:00',
    `start=${parsed.startTime} end=${parsed.endTime}`)

  // ---------- 3) 事由提取 ----------
  console.log('\n--- 3) reason extraction ---')
  parsed = flow.parseLeaveInput('因为感冒了', {})
  check('reason with indicator: 因为感冒了', parsed.reason === '感冒了', parsed.reason)

  parsed = flow.parseLeaveInput('由于家中有事', {})
  check('reason with indicator: 由于家中有事', parsed.reason === '家中有事', parsed.reason)

  parsed = flow.parseLeaveInput('我要请假', {})
  check('no reason in pure intent phrase', parsed.reason === undefined, parsed.reason)

  parsed = flow.parseLeaveInput('10月12日因为感冒就医', {})
  check('date + reason combined',
    parsed.startTime?.startsWith('2026-10-12') === true && parsed.reason === '感冒就医',
    `start=${parsed.startTime} reason=${parsed.reason}`)

  // ---------- 4) 追问生成 ----------
  console.log('\n--- 4) followup generation ---')
  check('isLeaveComplete: empty data', !flow.isLeaveComplete({}))
  check('isLeaveComplete: only startTime', !flow.isLeaveComplete({ startTime: '2026-10-12 08:00' }))
  check('isLeaveComplete: startTime + endTime', !flow.isLeaveComplete({ startTime: '2026-10-12 08:00', endTime: '2026-10-14 18:00' }))
  check('isLeaveComplete: all fields', flow.isLeaveComplete({ startTime: '2026-10-12 08:00', endTime: '2026-10-14 18:00', reason: '感冒' }))

  const followup1 = flow.getLeaveFollowup({})
  check('followup asks for startTime when empty', followup1?.includes('开始时间') === true, followup1?.slice(0, 40))

  const followup2 = flow.getLeaveFollowup({ startTime: '2026-10-12 08:00' })
  check('followup asks for endTime when only startTime', followup2?.includes('结束时间') === true, followup2?.slice(0, 40))

  const followup3 = flow.getLeaveFollowup({ startTime: '2026-10-12 08:00', endTime: '2026-10-14 18:00' })
  check('followup asks for reason when only times', followup3?.includes('事由') === true, followup3?.slice(0, 40))

  const followup4 = flow.getLeaveFollowup({ startTime: '2026-10-12 08:00', endTime: '2026-10-14 18:00', reason: '感冒' })
  check('followup is undefined when complete', followup4 === undefined, followup4)

  // ---------- 5) 直接调 API（真实后端）----------
  console.log('\n--- 5) direct API calls ---')
  let successResult = null
  let apiError = null
  let missingFieldError = null
  let timeOrderError = null

  try {
    // 生成唯一的事由以避免与之前的提交冲突
    const uniqueReason = `验证脚本测试-${Date.now()}`
    const tomorrowDate = new Date()
    tomorrowDate.setDate(tomorrowDate.getDate() + 1)
    const dayAfterDate = new Date()
    dayAfterDate.setDate(dayAfterDate.getDate() + 2)
    const startStr = `${tomorrowDate.getFullYear()}-${String(tomorrowDate.getMonth() + 1).padStart(2, '0')}-${String(tomorrowDate.getDate()).padStart(2, '0')} 08:00`
    const endStr = `${dayAfterDate.getFullYear()}-${String(dayAfterDate.getMonth() + 1).padStart(2, '0')}-${String(dayAfterDate.getDate()).padStart(2, '0')} 18:00`

    successResult = await leavesApi.submitLeaveApplication({
      studentId: STUDENT_ID,
      startTime: startStr,
      endTime: endStr,
      reason: uniqueReason,
    })
    console.log(`submit success -> id=${successResult.id} status=${successResult.status}`)
  } catch (e) {
    apiError = e
    console.log(`HTTP FAIL: ${e.name} / ${e.message}`)
  }

  // 缺字段 → 400
  try {
    await leavesApi.submitLeaveApplication({
      studentId: STUDENT_ID,
      startTime: '',
      endTime: '2026-10-14 18:00',
      reason: 'test',
    })
  } catch (e) {
    missingFieldError = e
  }

  // 时间倒置 → 400
  try {
    await leavesApi.submitLeaveApplication({
      studentId: STUDENT_ID,
      startTime: '2026-10-14 18:00',
      endTime: '2026-10-12 08:00',
      reason: 'test',
    })
  } catch (e) {
    timeOrderError = e
  }

  // ---------- 6) 编排层 + 断言 ----------
  console.log('\n--- 6) orchestration layer ---')

  if (!apiError && successResult) {
    check('submit returns object with id (number)', typeof successResult.id === 'number', `id=${successResult.id}`)
    check('submit returns studentId', successResult.studentId === STUDENT_ID, successResult.studentId)
    check('submit returns status=PENDING', successResult.status === 'PENDING', successResult.status)
    check('submit returns reason matches', successResult.reason?.includes('验证脚本测试'), successResult.reason)
    check('submit returns startTime as ISO string', typeof successResult.startTime === 'string', successResult.startTime)
    check('submit returns createdAt as ISO string', typeof successResult.createdAt === 'string', successResult.createdAt)

    // 编排层
    const completeData = {
      startTime: successResult.startTime?.replace('T', ' ').slice(0, 16),
      endTime: successResult.endTime?.replace('T', ' ').slice(0, 16),
      reason: successResult.reason,
    }
    const payload = await flow.submitLeave(completeData)
    console.log(`submitLeave payload: error=${payload.error === true}`)
    console.log(`content (first 80 chars): ${payload.content.slice(0, 80)}…`)

    check('success payload contains 提交成功', payload.content.includes('提交成功'), payload.content.slice(0, 40))
    check('success payload contains the application id', /申请编号：\d+/.test(payload.content), payload.content.match(/申请编号：(\d+)/)?.[0] ?? 'not found')
    check('success payload has no error flag', payload.error !== true)
    check('payload is renderable', flow.shouldRenderReply(payload))

    const message = flow.toAssistantMessage(payload)
    check('toAssistantMessage keeps role/content', message.role === 'assistant' && message.content === payload.content)
    check('toAssistantMessage has no error flag on success', message.error !== true)
  } else {
    check('API reachable (run with backend running)', false, apiError?.message ?? 'unknown')
  }

  // 400 错误
  check('missing field -> LeaveApiError with kind=http',
    missingFieldError?.name === 'LeaveApiError' && missingFieldError?.kind === 'http',
    `name=${missingFieldError?.name} kind=${missingFieldError?.kind}`)
  check('missing field -> status 400', missingFieldError?.status === 400, `status=${missingFieldError?.status}`)
  check('missing field -> serverMessage present',
    typeof missingFieldError?.serverMessage === 'string' && missingFieldError.serverMessage.length > 0,
    missingFieldError?.serverMessage)
  check('missing field -> message contains server message',
    missingFieldError?.message?.includes(missingFieldError?.serverMessage ?? '___'),
    missingFieldError?.message)

  check('time order -> LeaveApiError with kind=http',
    timeOrderError?.name === 'LeaveApiError' && timeOrderError?.kind === 'http',
    `name=${timeOrderError?.name} kind=${timeOrderError?.kind}`)
  check('time order -> status 400', timeOrderError?.status === 400, `status=${timeOrderError?.status}`)
  check('time order -> message mentions time order',
    timeOrderError?.serverMessage?.includes('结束时间') || timeOrderError?.serverMessage?.includes('早于'),
    timeOrderError?.serverMessage)

  // ---------- 7) F3-1/F3-2/F3-3 回归 ----------
  console.log('\n--- 7) F3-1/F3-2/F3-3 regression ---')
  const examFlow = await server.ssrLoadModule('/src/lib/examFlow.ts')
  const courseFlow = await server.ssrLoadModule('/src/lib/courseFlow.ts')
  const scoreFlow = await server.ssrLoadModule('/src/lib/scoreFlow.ts')

  check('examFlow still exports runExamQuery', typeof examFlow.runExamQuery === 'function')
  check('examFlow still exports shouldRenderReply', typeof examFlow.shouldRenderReply === 'function')
  check('courseFlow still exports runCourseQuery', typeof courseFlow.runCourseQuery === 'function')
  check('scoreFlow still exports runScoreQuery', typeof scoreFlow.runScoreQuery === 'function')
  check('scoreFlow still exports SCORE_SEMESTER_FOLLOWUP', typeof scoreFlow.SCORE_SEMESTER_FOLLOWUP === 'string')

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== summary: ${results.length - failed.length}/${results.length} passed ===`)
  process.exitCode = results.length === 0 ? 1 : failed.length === 0 ? 0 : 1
} finally {
  await server.close()
}
