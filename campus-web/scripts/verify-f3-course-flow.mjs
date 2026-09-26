/**
 * F3-2 课程链路验证（真实模块 + 真实 HTTP，无浏览器）。
 *
 * 与 `verify-f3-exam-flow.mjs` 同款姿势：用 Vite 的 SSR 模块加载跑真实的
 * `src/` 模块（不是复制品），通过 `fetch` 打真实 HTTP，验证：
 *   1. 意图识别与周次抽取（含「抽不到 = undefined」的边界）；
 *   2. 直接调 API：week=3 / week=8 / 不传 week / 不存在学生 / 缺参与非数字 week
 *      的后端真实行为（week 过滤真的生效：「数据库原理」5-16 周，
 *      第 3 周查不到、第 8 周查得到）；
 *   3. 编排层（组件真正调用的入口）：成功文案、周次措辞、绝不编造数据。
 *
 * 用法（不能带 configFile，SSR 下会触发 dep-scan 报错）：
 *   BASE_URL=http://localhost:8081 node scripts/verify-f3-course-flow.mjs
 */

import { createServer } from 'vite'

const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080'
const STUDENT_ID = process.env.STUDENT_ID ?? 'student-001'

// 注入环境变量：Vite 在 SSR 加载时会替换 import.meta.env.VITE_*
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
  const intent = await server.ssrLoadModule('/src/constants/courseIntent.ts')
  const examIntent = await server.ssrLoadModule('/src/constants/examIntent.ts')
  const coursesApi = await server.ssrLoadModule('/src/api/courses.ts')
  const flow = await server.ssrLoadModule('/src/lib/courseFlow.ts')

  console.log('=== F3-2 course query chain verification ===')
  console.log(`BaseURL=${env.API_BASE_URL} StudentId=${env.resolveStudentId()} Path=${coursesApi.COURSES_PATH}`)

  // ---------- 1) 意图路由与周次抽取 ----------
  console.log('\n--- 1) intent routing & week parsing ---')
  const courseQuestions = [
    '\u5e2e\u6211\u67e5\u4e00\u4e0b\u8fd9\u5468\u7684\u8bfe\u7a0b', // 帮我查一下这周的课程
    '\u67e5\u770b\u7b2c3\u5468\u8bfe\u7a0b',                        // 查看第3周课程
    '\u6211\u7684\u8bfe\u7a0b\u8868',                               // 我的课程表
    '\u6211\u8fd9\u5468\u6709\u54ea\u4e9b\u8bfe\uff1f',             // 我这周有哪些课？
    '\u660e\u5929\u4e0a\u8bfe\u5417',                               // 明天上课吗
  ]
  courseQuestions.forEach((q) => check(`course question hits course intent: ${q}`, intent.isCourseQuery(q)))

  check('exam question does NOT hit course intent', !intent.isCourseQuery('帮我查一下考试安排'))
  check('leave question does NOT hit course intent', !intent.isCourseQuery('我想请事假，需要提供哪些信息？'))
  check('exam intent regression: exam question still hits exam intent', examIntent.isExamQuery('帮我查一下考试安排'))
  check('course question does NOT hit exam intent', !examIntent.isExamQuery('查看第3周课程'))

  check('week parsed from "查看第3周课程"', intent.parseCourseWeek('查看第3周课程') === 3)
  check('week parsed with spaces "第 13 周"', intent.parseCourseWeek('第 13 周') === 13)
  check('week parsed from "第8周的课表"', intent.parseCourseWeek('第8周的课表') === 8)
  check('no week in "我的课程表" -> undefined', intent.parseCourseWeek('我的课程表') === undefined)
  check('no week in "这周的课程" -> undefined', intent.parseCourseWeek('这周的课程') === undefined)
  check('"第几周" (no digits) -> undefined', intent.parseCourseWeek('现在第几周有课') === undefined)
  check('three-digit week "第100周" -> undefined', intent.parseCourseWeek('第100周的课') === undefined)

  // ---------- 2) 直接调 API（真实后端）----------
  console.log('\n--- 2) direct API calls ---')
  let week3 = null
  let week8 = null
  let semester = null
  let emptyStudent = null
  let apiError = null
  try {
    week3 = await coursesApi.fetchCourseSchedules({ studentId: STUDENT_ID, week: 3 })
    week8 = await coursesApi.fetchCourseSchedules({ studentId: STUDENT_ID, week: 8 })
    semester = await coursesApi.fetchCourseSchedules({ studentId: STUDENT_ID })
    emptyStudent = await coursesApi.fetchCourseSchedules({ studentId: 'student-999', week: 3 })
    console.log(`week=3 -> ${week3.length} item(s)`)
    console.log(`week=8 -> ${week8.length} item(s)`)
    console.log(`no week -> ${semester.length} item(s)`)
    console.log(`student-999 week=3 -> ${emptyStudent.length} item(s)`)
  } catch (e) {
    apiError = e
    console.log(`HTTP FAIL: ${e.name} / ${e.message}`)
  }

  // ---------- 3) 后端参数校验行为（绕过前端模块，直接打原始接口）----------
  console.log('\n--- 3) raw backend parameter behavior ---')
  const rawStatus = async (path) => {
    const res = await fetch(`${BASE_URL}${path}`, { headers: { Accept: 'application/json' } })
    return res.status
  }
  const missingStudentIdStatus = await rawStatus('/api/courses').catch(() => 'FETCH-ERROR')
  const badWeekStatus = await rawStatus('/api/courses?studentId=student-001&week=abc').catch(() => 'FETCH-ERROR')
  check('missing studentId -> HTTP 400 (backend contract)', missingStudentIdStatus === 400, `status=${missingStudentIdStatus}`)
  check('non-numeric week -> HTTP 400 (backend contract)', badWeekStatus === 400, `status=${badWeekStatus}`)

  // ---------- 4) 编排层 + 断言 ----------
  console.log('\n--- 4) orchestration layer (the entry the component calls) ---')
  const payloadWeek3 = await flow.runCourseQuery(3)
  console.log(`role=${payloadWeek3.role} error=${payloadWeek3.error === true}`)
  console.log(`content (week=3):\n${payloadWeek3.content}`)

  const payloadSemester = await flow.runCourseQuery(undefined)
  console.log(`content (no week, first 60 chars): ${payloadSemester.content.slice(0, 60)}…`)

  console.log('\n--- 5) assertions ---')
  if (!apiError && week3) {
    check('student-001 + week=3 HAS data', week3.length > 0, `count=${week3.length}`)
    check('structure is 6 string fields (course/teacher/weekday/time/room/weeks)',
      week3.every(
        (x) =>
          typeof x.course === 'string' &&
          typeof x.teacher === 'string' &&
          typeof x.weekday === 'string' &&
          typeof x.time === 'string' &&
          typeof x.room === 'string' &&
          typeof x.weeks === 'string',
      ))
    check('no leaked entity fields (id/studentId/createdAt/classTime/weekStart/weekEnd)',
      week3.every(
        (x) => !('id' in x) && !('studentId' in x) && !('createdAt' in x) &&
          !('classTime' in x) && !('weekStart' in x) && !('weekEnd' in x),
      ))
    check('week filter really active: week=8 returns MORE than week=3',
      week8.length > week3.length, `week3=${week3.length} week8=${week8.length}`)
    check('week param reaches the DB: 数据库原理 (5-16周) missing at week=3',
      !week3.some((x) => x.course === '数据库原理'))
    check('week param reaches the DB: 数据库原理 (5-16周) present at week=8',
      week8.some((x) => x.course === '数据库原理'))
    check('no week -> whole semester (>= week=8 rows)', semester.length >= week8.length,
      `semester=${semester.length}`)
    check('nonexistent student returns EMPTY ARRAY (not error, not 404)',
      Array.isArray(emptyStudent) && emptyStudent.length === 0)

    check('week=3 reply carries the week scope wording', payloadWeek3.content.includes('第 3 周'))
    check('every real week-3 row appears in the chat text',
      week3.every(
        (x) =>
          payloadWeek3.content.includes(x.course) &&
          payloadWeek3.content.includes(x.room) &&
          payloadWeek3.content.includes(x.weekday) &&
          payloadWeek3.content.includes(x.time),
      ))
    check('数据库原理 NOT in week=3 reply (no fabrication beyond real data)',
      !payloadWeek3.content.includes('数据库原理'))
    check('no error flag on success', payloadWeek3.error !== true)
    check('semester reply carries the whole-semester wording', payloadSemester.content.includes('整学期'))
    check('semester reply includes 数据库原理', payloadSemester.content.includes('数据库原理'))
    check('payload is renderable', flow.shouldRenderReply(payloadWeek3))

    const message = flow.toAssistantMessage(payloadWeek3)
    check('toAssistantMessage keeps role/content/error', message.role === 'assistant' &&
      message.content === payloadWeek3.content && message.error !== true)
  } else {
    check('API reachable (run with BASE_URL pointing at the real backend)', false, apiError?.message ?? 'unknown')
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== summary: ${results.length - failed.length}/${results.length} passed ===`)
  process.exitCode = failed.length === 0 ? 0 : 1
} finally {
  await server.close()
}
