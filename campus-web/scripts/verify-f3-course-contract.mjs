/**
 * F3-2 静态契约核对（课程表接入）。
 *
 * 与 `verify-f3-contract.mjs` 同款思路：直接断言「源码里的事实」，
 * 而不是靠运行时观察 —— 每条都是可复现的字符串/结构判定。
 *
 * 用法：node scripts/verify-f3-course-contract.mjs
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
const courses = read('src/api/courses.ts')
const courseFlow = read('src/lib/courseFlow.ts')
const courseIntent = read('src/constants/courseIntent.ts')
const typesCourse = read('src/types/course.ts')
const mockReply = read('src/lib/mockReply.ts')
const pkg = JSON.parse(read('package.json'))

console.log('=== F3-2 source contract check (courses) ===')

// 1) 组件不得直接发请求
check('ChatView does not call axios/fetch directly',
  !/axios\.get|http\.get|fetch\s*\(/.test(code('src/views/ChatView.vue')))

// 2) 意图分流：课程问题在 mock 之前被分流；考试分支保持 F3-1 原样
check('ChatView imports courseFlow', chatView.includes("from '@/lib/courseFlow'"))
check('ChatView imports courseIntent (isCourseQuery + parseCourseWeek)',
  chatView.includes("from '@/constants/courseIntent'"))
check('ChatView branches on isCourseQuery before mock',
  /if \(isCourseQuery\(text\)\) \{\s*replyWithCourses\(parseCourseWeek\(text\)\)\s*return\s*\}/.test(chatView))
check('F3-1 exam branch is unchanged',
  /if \(isExamQuery\(text\)\) \{\s*replyWithExams\(\)\s*return\s*\}/.test(chatView))
check('mock reply still runs in the final fallback branch', /replyWithMock\(text\)/.test(chatView))

// 3) 失败文案与假数据
check('failure wording "课表查询失败" lives in the API layer', courses.includes('课表查询失败'))
check('courseFlow never fabricates course rows',
  !/数据结构|计算机网络|高等数学|数据库原理|A302|李明|周一/.test(courseFlow))

// 4) 契约来源可追溯（不猜字段）
check('types/course.ts cites the backend DTO file name',
  typesCourse.includes('CourseScheduleResponse.java'))
check('types/course.ts declares exactly course/teacher/weekday/time/room/weeks',
  ['course', 'teacher', 'weekday', 'time', 'room', 'weeks'].every((f) => typesCourse.includes(f)))
check('types/course.ts documents "no wrapper" (not ApiResponse)',
  typesCourse.includes('List<CourseScheduleResponse>'))

// 5) week / studentId 不在业务模块里写死
check('courses.ts has no hardcoded studentId', !code('src/api/courses.ts').includes('student-001'))
check('courses.ts has no hardcoded week default', !/week\s*=\s*\d/.test(code('src/api/courses.ts')))
check('week only appended to the URL when explicitly numeric',
  code('src/api/courses.ts').includes('typeof query.week'))
check('runCourseQuery takes week as an explicit parameter without default',
  /runCourseQuery\(\s*week: number \| undefined/.test(courseFlow) &&
    !/runCourseQuery\(\s*week[^)]*=\s*\d/.test(courseFlow))
check('studentId still centralized in env.ts (resolveStudentId)',
  courseFlow.includes('resolveStudentId()') && read('src/config/env.ts').includes('VITE_CAMPUS_STUDENT_ID'))

// 6) mockReply 不再声称课表未接入
check('mockReply declares courses are no longer handled here',
  mockReply.includes('课表查询已经接通真实后端接口'))

// 7) 未引入登录 / UI 组件库（阶段边界）
check('no JWT/login implementation added',
  !/jwt|Authorization|Bearer/i.test(
    code('src/api/courses.ts') + code('src/lib/courseFlow.ts') + code('src/constants/courseIntent.ts'),
  ))
const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies })
check('no new UI component library added',
  !deps.some((d) => /element|antd|naive|vuetify|primevue|tailwind/i.test(d)))

const failed = results.filter((r) => !r.ok)
console.log(`\n=== course source contract: ${results.length - failed.length}/${results.length} passed ===`)
process.exitCode = failed.length ? 1 : 0
