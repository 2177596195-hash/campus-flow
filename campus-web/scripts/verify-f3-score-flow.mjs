/**
 * F3-3 成绩链路验证（真实模块 + 真实 HTTP，无浏览器）。
 *
 * 与 `verify-f3-exam-flow.mjs` / `verify-f3-course-flow.mjs` 同款姿势：
 * 用 Vite 的 SSR 模块加载跑真实的 `src/` 模块（不是复制品），
 * 通过 `fetch` 打真实 HTTP，验证：
 *   1. 成绩意图识别（含与考试 / 课程意图的边界）；
 *   2. 学期解析：两种明确写法 → 后端格式；相对说法 / 缺年份 → undefined（不猜）；
 *   3. 直接调 API：有数据学期 / 全部学期 / 不存在学期（200+[]）/
 *      不存在学生（200+[]）/ 缺 studentId（400）/ 空白学期（视为未提供）/
 *      非法学期（后端无校验 → 200+[]）——全部对着真实后端断言；
 *   4. 编排层（组件真正调用的入口）：成功文案、学期措辞、追问文案、
 *      空数据正常回复、绝不编造数据；
 *   5. score / credit 是数值（与考试 / 课程全 string 契约的关键差异）。
 *
 * 用法（不能带 configFile，SSR 下会触发 dep-scan 报错）：
 *   BASE_URL=http://localhost:8081 node scripts/verify-f3-score-flow.mjs
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
  const intent = await server.ssrLoadModule('/src/constants/scoreIntent.ts')
  const examIntent = await server.ssrLoadModule('/src/constants/examIntent.ts')
  const courseIntent = await server.ssrLoadModule('/src/constants/courseIntent.ts')
  const scoresApi = await server.ssrLoadModule('/src/api/scores.ts')
  const flow = await server.ssrLoadModule('/src/lib/scoreFlow.ts')

  console.log('=== F3-3 score query chain verification ===')
  console.log(`BaseURL=${env.API_BASE_URL} StudentId=${env.resolveStudentId()} Path=${scoresApi.SCORES_PATH}`)

  // ---------- 1) 意图路由 ----------
  console.log('\n--- 1) intent routing ---')
  const scoreQuestions = [
    '查一下我的成绩',      // 用户示例 1
    '查询成绩',            // 用户示例 2
    '看看考试成绩',        // 用户示例 3：含「考试」，必须仍进成绩分支
    '我的成绩怎么样',      // 用户示例 4
    '考了多少分',
    '我挂科了吗',
    '帮我查一下上学期的成绩', // 欢迎页示例卡片（相对学期 → 该追问而非直查）
  ]
  scoreQuestions.forEach((q) => check(`score question hits score intent: ${q}`, intent.isScoreQuery(q)))

  check('exam question does NOT hit score intent', !intent.isScoreQuery('帮我查一下考试安排'))
  check('course question does NOT hit score intent', !intent.isScoreQuery('查看第3周课程'))
  check('leave question does NOT hit score intent', !intent.isScoreQuery('我想请事假，需要提供哪些信息？'))
  check('exam intent regression: exam question still hits exam intent', examIntent.isExamQuery('帮我查一下考试安排'))
  check('course intent regression: course question still hits course intent', courseIntent.isCourseQuery('查看第3周课程'))
  check('"看看考试成绩" would be hijacked by exam intent if routed after it — exam intent also matches it',
    examIntent.isExamQuery('看看考试成绩')) // 这正是成绩分支必须在前的原因（事实记录，非行为断言）

  // ---------- 2) 学期解析（明确写法才解析；其余一律 undefined 交给追问）----------
  console.log('\n--- 2) semester parsing ---')
  check('literal format "2025-2026-1"', intent.parseSemester('2025-2026-1') === '2025-2026-1')
  check('literal inside a sentence', intent.parseSemester('查我的2025-2026-1学期成绩') === '2025-2026-1')
  check('Chinese "2026-2027第一学期" (spec example)',
    intent.parseSemester('查询2026-2027第一学期成绩') === '2026-2027-1')
  check('Chinese with 学年 "2025-2026学年第1学期"',
    intent.parseSemester('2025-2026学年第1学期') === '2025-2026-1')
  check('Chinese separator 至 "2025至2026学年第二学期"',
    intent.parseSemester('2025至2026学年第二学期') === '2025-2026-2')
  check('spaces tolerated "第 一 学期"', intent.parseSemester('2025-2026 第 一 学期') === '2025-2026-1')
  check('relative "上学期" -> undefined (no calendar guessing)', intent.parseSemester('帮我查一下上学期的成绩') === undefined)
  check('relative "这学期" -> undefined', intent.parseSemester('这学期成绩') === undefined)
  check('"第一学期" without years -> undefined', intent.parseSemester('第一学期成绩') === undefined)
  check('three-digit semester "2025-2026-12" -> undefined', intent.parseSemester('2025-2026-12') === undefined)
  check('"全部" -> wantsAllSemesters (explicit choice, not a guess)', intent.wantsAllSemesters('全部'))
  check('"所有学期" -> wantsAllSemesters', intent.wantsAllSemesters('查我所有学期的成绩'))
  check('a semester answer does NOT trigger wantsAllSemesters', !intent.wantsAllSemesters('2025-2026-1'))
  check('a plain score question does NOT trigger wantsAllSemesters', !intent.wantsAllSemesters('查一下我的成绩'))

  // ---------- 3) 直接调 API（真实后端）----------
  console.log('\n--- 3) direct API calls ---')
  let sem1 = null
  let all = null
  let emptySemester = null
  let emptyStudent = null
  let apiError = null
  try {
    sem1 = await scoresApi.fetchScoreRecords({ studentId: STUDENT_ID, semester: '2025-2026-1' })
    all = await scoresApi.fetchScoreRecords({ studentId: STUDENT_ID })
    emptySemester = await scoresApi.fetchScoreRecords({ studentId: STUDENT_ID, semester: '2026-2027-1' })
    emptyStudent = await scoresApi.fetchScoreRecords({ studentId: 'student-999', semester: '2025-2026-1' })
    console.log(`semester=2025-2026-1 -> ${sem1.length} item(s)`)
    console.log(`no semester -> ${all.length} item(s)`)
    console.log(`semester=2026-2027-1 -> ${emptySemester.length} item(s)`)
    console.log(`student-999 -> ${emptyStudent.length} item(s)`)
  } catch (e) {
    apiError = e
    console.log(`HTTP FAIL: ${e.name} / ${e.message}`)
  }

  // ---------- 4) 后端参数校验行为（绕过前端模块，直接打原始接口）----------
  console.log('\n--- 4) raw backend parameter behavior ---')
  const rawStatus = async (path) => {
    const res = await fetch(`${BASE_URL}${path}`, { headers: { Accept: 'application/json' } })
    return res.status
  }
  const rawJson = async (path) => {
    const res = await fetch(`${BASE_URL}${path}`, { headers: { Accept: 'application/json' } })
    return res.json()
  }
  const missingStudentIdStatus = await rawStatus('/api/scores').catch(() => 'FETCH-ERROR')
  const blankSemesterStatus = await rawStatus('/api/scores?studentId=student-001&semester=').catch(() => 'FETCH-ERROR')
  const badSemesterStatus = await rawStatus('/api/scores?studentId=student-001&semester=abc').catch(() => 'FETCH-ERROR')
  const badSemesterBody = await rawJson('/api/scores?studentId=student-001&semester=abc').catch(() => null)
  const blankSemesterBody = await rawJson('/api/scores?studentId=student-001&semester=').catch(() => null)
  check('missing studentId -> HTTP 400 (backend contract)', missingStudentIdStatus === 400, `status=${missingStudentIdStatus}`)
  check('blank semester -> HTTP 200 (backend treats blank as not provided)', blankSemesterStatus === 200, `status=${blankSemesterStatus}`)
  check('blank semester -> same rows as no semester (all 5 for student-001)',
    Array.isArray(blankSemesterBody) && blankSemesterBody.length === 5, `count=${blankSemesterBody?.length}`)
  check('illegal semester "abc" -> HTTP 200 + [] (backend has NO format validation)',
    badSemesterStatus === 200 && Array.isArray(badSemesterBody) && badSemesterBody.length === 0,
    `status=${badSemesterStatus}`)

  // ---------- 5) 编排层 + 断言 ----------
  console.log('\n--- 5) orchestration layer (the entry the component calls) ---')
  const payloadSem = await flow.runScoreQuery('2025-2026-1')
  console.log(`role=${payloadSem.role} error=${payloadSem.error === true}`)
  console.log(`content (semester=2025-2026-1):\n${payloadSem.content}\n`)

  const payloadAll = await flow.runScoreQuery(undefined)
  console.log(`content (all semesters, first 60 chars): ${payloadAll.content.slice(0, 60)}…`)

  const payloadEmpty = await flow.runScoreQuery('2026-2027-1')
  console.log(`content (empty semester):\n${payloadEmpty.content}\n`)

  console.log('\n--- 6) assertions ---')
  if (!apiError && sem1) {
    check('student-001 + 2025-2026-1 HAS data (3 rows)', sem1.length === 3, `count=${sem1.length}`)
    check('structure: semester/course are strings, score/credit are NUMBERS',
      sem1.every(
        (x) =>
          typeof x.semester === 'string' &&
          typeof x.course === 'string' &&
          typeof x.score === 'number' &&
          typeof x.credit === 'number',
      ))
    check('no leaked entity fields (id/studentId/createdAt)',
      sem1.every((x) => !('id' in x) && !('studentId' in x) && !('createdAt' in x)))
    check('real values survive the number round-trip: 操作系统 95 / 数据结构 92 / 计算机网络 88',
      sem1.some((x) => x.course === '操作系统' && x.score === 95 && x.credit === 3.5) &&
        sem1.some((x) => x.course === '数据结构' && x.score === 92 && x.credit === 4) &&
        sem1.some((x) => x.course === '计算机网络' && x.score === 88 && x.credit === 3))
    check('no semester -> whole history (5 rows for student-001)', all.length === 5, `count=${all.length}`)
    check('no semester -> backend ordered semesters newest first (2025-2026-1 before 2024-2025-2)',
      all[0].semester === '2025-2026-1' && all[all.length - 1].semester === '2024-2025-2',
      `first=${all[0].semester} last=${all[all.length - 1].semester}`)
    check('nonexistent semester returns EMPTY ARRAY (200, not error, not 404)',
      Array.isArray(emptySemester) && emptySemester.length === 0)
    check('nonexistent student returns EMPTY ARRAY (200, not error)',
      Array.isArray(emptyStudent) && emptyStudent.length === 0)

    check('semester reply carries the scope wording', payloadSem.content.includes('2025-2026-1 学期成绩'))
    check('semester reply counts the rows', payloadSem.content.includes('共 3 门'))
    check('every real row appears in the chat text (course + score + credit)',
      sem1.every(
        (x) =>
          payloadSem.content.includes(x.course) &&
          payloadSem.content.includes(`成绩：${x.score}`) &&
          payloadSem.content.includes(`学分：${x.credit}`),
      ))
    check('no error flag on success', payloadSem.error !== true)
    check('all-semester reply carries the wording 全部学期成绩', payloadAll.content.includes('全部学期成绩'))
    check('all-semester reply includes the older semester 2024-2025-2 and 高等数学',
      payloadAll.content.includes('2024-2025-2') && payloadAll.content.includes('高等数学'))
    check('empty semester reply is a NORMAL answer (该学期暂无成绩记录)',
      payloadEmpty.content.includes('2026-2027-1') && payloadEmpty.content.includes('该学期暂无成绩记录'),
      payloadEmpty.content.slice(0, 80))
    check('empty semester reply has no error flag', payloadEmpty.error !== true)
    check('empty semester reply contains NO fabricated scores',
      !['数据结构', '计算机网络', '操作系统', '92', '88', '95'].some((k) => payloadEmpty.content.includes(k)))
    check('payload is renderable', flow.shouldRenderReply(payloadSem))

    const message = flow.toAssistantMessage(payloadSem)
    check('toAssistantMessage keeps role/content/error', message.role === 'assistant' &&
      message.content === payloadSem.content && message.error !== true)

    check('SCORE_SEMESTER_FOLLOWUP asks for the semester (no request needed)',
      flow.SCORE_SEMESTER_FOLLOWUP.includes('学期') &&
        flow.SCORE_SEMESTER_FOLLOWUP.includes('2025-2026-1') &&
        flow.SCORE_SEMESTER_FOLLOWUP.includes('全部'))
  } else {
    check('API reachable (run with BASE_URL pointing at the real backend)', false, apiError?.message ?? 'unknown')
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== summary: ${results.length - failed.length}/${results.length} passed ===`)
  process.exitCode = failed.length === 0 ? 0 : 1
} finally {
  await server.close()
}
