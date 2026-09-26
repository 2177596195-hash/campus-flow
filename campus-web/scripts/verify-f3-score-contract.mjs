/**
 * F3-3 静态契约核对（成绩接入）。
 *
 * 与 `verify-f3-contract.mjs` / `verify-f3-course-contract.mjs` 同款思路：
 * 直接断言「源码里的事实」，而不是靠运行时观察 —— 每条都是可复现的
 * 字符串/结构判定。
 *
 * 用法：node scripts/verify-f3-score-contract.mjs
 */

import { readFileSync } from 'node:fs'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}
const read = (p) => readFileSync(p, 'utf8')
/** 去掉注释行，避免「注释里说不要做 X」被误判成「做了 X」 */
const code = (p) =>
  read(p)
    .split(/\r?\n/)
    .filter((line) => !/^\s*(\/\/|\/\*|\*)/.test(line))
    .join('\n')

const chatView = read('src/views/ChatView.vue')
const scores = read('src/api/scores.ts')
const scoreFlow = read('src/lib/scoreFlow.ts')
const scoreIntent = read('src/constants/scoreIntent.ts')
const typesScore = read('src/types/score.ts')
const mockReply = read('src/lib/mockReply.ts')
const pkg = JSON.parse(read('package.json'))

console.log('=== F3-3 source contract check (scores) ===')

// 1) 组件不得直接发请求
check('ChatView does not call axios/fetch directly',
  !/axios\.get|http\.get|fetch\s*\(/.test(code('src/views/ChatView.vue')))

// 2) 意图分流：成绩分支存在且排在考试分支之前（「考试成绩」问的是分数）；
//    考试 / 课程分支代码保持 F3-1 / F3-2 原样
check('ChatView imports scoreFlow (runScoreQuery + followup)',
  chatView.includes("from '@/lib/scoreFlow'"))
check('ChatView imports scoreIntent (isScoreQuery + parseSemester + wantsAllSemesters)',
  chatView.includes("from '@/constants/scoreIntent'"))
const scoreBranchPos = chatView.indexOf('isScoreQuery(text)')
const examBranchPos = chatView.search(/if \(isExamQuery\(text\)\)/)
check('score branch is routed BEFORE the exam branch',
  scoreBranchPos >= 0 && examBranchPos >= 0 && scoreBranchPos < examBranchPos,
  `score@${scoreBranchPos} exam@${examBranchPos}`)
check('F3-1 exam branch is unchanged',
  /if \(isExamQuery\(text\)\) \{\s*replyWithExams\(\)\s*return\s*\}/.test(chatView))
check('F3-2 course branch is unchanged',
  /if \(isCourseQuery\(text\)\) \{\s*replyWithCourses\(parseCourseWeek\(text\)\)\s*return\s*\}/.test(chatView))
check('mock reply still runs in the final fallback branch', /replyWithMock\(text\)/.test(chatView))

// 3) 学期槽位：没给学期不发请求，只回一句追问；槽位在新建/清空会话时复位
check('ChatView arms the semester slot instead of guessing a default',
  chatView.includes('pendingScoreSemester = true') && chatView.includes('askScoreSemester()'))
check('ChatView answers the followup only via SCORE_SEMESTER_FOLLOWUP',
  chatView.includes('SCORE_SEMESTER_FOLLOWUP') && !chatView.includes('要查哪个学期'))
check('semester slot is reset on new/clear conversation',
  /function handleNewConversation\(\) \{\s*pendingScoreSemester = false/.test(chatView) &&
    /function handleClearConversation\(\) \{[\s\S]*pendingScoreSemester = false/.test(chatView))
check('slot follow-up input still resolves "全部" to a real query',
  chatView.includes('wantsAllSemesters(text)'))

// 4) 失败文案与假数据
check('failure wording "成绩查询失败" lives in the API layer', scores.includes('成绩查询失败'))
check('scoreFlow never fabricates score rows',
  !/数据结构|计算机网络|操作系统|高等数学|大学英语|大学物理|线性代数/.test(scoreFlow))
check('followup mentions the semester FORMAT (UX hint) but leaks no real score values',
  scoreFlow.includes('2025-2026-1') && !/成绩：\d|学分：\d/.test(scoreFlow))

// 5) 契约来源可追溯（不猜字段）
check('types/score.ts cites the backend DTO file name',
  typesScore.includes('ScoreResponse.java'))
check('types/score.ts declares exactly semester/course/score/credit',
  ['semester', 'course', 'score', 'credit'].every((f) => typesScore.includes(f)))
check('types/score.ts documents "no wrapper" (not ApiResponse)',
  typesScore.includes('List<ScoreResponse>'))
check('score/credit are NUMBERS in the type (not strings) — key contract difference',
  /score: number/.test(code('src/types/score.ts')) && /credit: number/.test(code('src/types/score.ts')))
check('scores.ts validates score/credit as finite numbers',
  code('src/api/scores.ts').includes("typeof record.score === 'number'") &&
    code('src/api/scores.ts').includes('Number.isFinite(record.score)') &&
    code('src/api/scores.ts').includes("typeof record.credit === 'number'") &&
    code('src/api/scores.ts').includes('Number.isFinite(record.credit)'))

// 6) studentId / semester 不在业务模块里写死
check('scores.ts has no hardcoded studentId', !code('src/api/scores.ts').includes('student-001'))
check('scoreFlow has no hardcoded studentId', !code('src/lib/scoreFlow.ts').includes('student-001'))
check('semester only appended to the URL when explicitly non-empty',
  code('src/api/scores.ts').includes('typeof query.semester'))
check('runScoreQuery takes semester as an explicit parameter without default',
  /runScoreQuery\(\s*semester: string \| undefined/.test(scoreFlow) &&
    !/runScoreQuery\([^)]*semester[^)]*=\s*['"]/.test(scoreFlow))
check('scoreIntent parses only explicit semesters (no "上学期" guessing)',
  scoreIntent.includes('不解析「这学期 / 上学期 / 下学期」') &&
    /function parseSemester/.test(scoreIntent))
check('studentId still centralized in env.ts (resolveStudentId)',
  scoreFlow.includes('resolveStudentId()') && read('src/config/env.ts').includes('VITE_CAMPUS_STUDENT_ID'))

// 7) mockReply 不再声称成绩未接入
check('mockReply declares scores are no longer handled here',
  mockReply.includes('成绩查询已经接通真实后端接口'))

// 8) 未引入登录 / UI 组件库（阶段边界）
check('no JWT/login implementation added',
  !/jwt|Authorization|Bearer/i.test(
    code('src/api/scores.ts') + code('src/lib/scoreFlow.ts') + code('src/constants/scoreIntent.ts'),
  ))
const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies })
check('no new UI component library added',
  !deps.some((d) => /element|antd|naive|vuetify|primevue|tailwind/i.test(d)))

const failed = results.filter((r) => !r.ok)
console.log(`\n=== score source contract: ${results.length - failed.length}/${results.length} passed ===`)
process.exitCode = failed.length ? 1 : 0
