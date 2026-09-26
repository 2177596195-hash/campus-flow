/**
 * F3-2 失败路径验证（真实模块 + 真实 HTTP，无浏览器）。
 *
 * 与 `verify-f3-failure-path.mjs` 同款姿势：用 Vite 的 SSR 能力即时转译
 * `src/**` 的真实 TS 模块，把 `buildApiUrl` 指向一个**确定不可达**的地址，
 * 然后断言：
 *   1) `runCourseQuery` **不抛异常**（链路层负责把错误变成一句话），
 *      无论 week 是数字还是 undefined；
 *   2) 返回的回复**带 `error` 标记**，且文案含统一失败前缀「课表查询失败」；
 *   3) 回复里**不含任何编造的课程数据**（课程 / 教师 / 教室 / 星期一个都不能出现）；
 *   4) `toAssistantMessage` 产出的消息可直接进 store，不回退成 mock 回复。
 *
 * 用法：node scripts/verify-f3-course-failure-path.mjs
 */

import { createServer } from 'vite'
import { pathToFileURL } from 'node:url'

const ROOT = 'D:/code/campus-flow/campus-web'
const UNREACHABLE = 'http://127.0.0.1:59999'

const server = await createServer({
  root: ROOT,
  logLevel: 'error',
  server: { middlewareMode: true },
  appType: 'custom',
  // 关键：让模块级常量在被导入时就指向不可达地址
  define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify(UNREACHABLE) },
})

const report = []
const check = (name, ok, detail = '') => {
  report.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}

try {
  const load = async (p) => server.ssrLoadModule(p)

  const env = await load('/src/config/env.ts')
  const flow = await load('/src/lib/courseFlow.ts')
  const coursesApi = await load('/src/api/courses.ts')
  const storeMod = await load('/src/stores/conversation.ts')

  console.log(`API_BASE_URL = ${JSON.stringify(env.API_BASE_URL)}`)
  check('base URL points at the unreachable port', env.API_BASE_URL === UNREACHABLE, env.API_BASE_URL)
  check(
    'built URL targets the unreachable port',
    env.buildApiUrl('/api/courses?studentId=student-001&week=3').startsWith(UNREACHABLE),
    env.buildApiUrl('/api/courses?studentId=student-001&week=3'),
  )

  // ---- 1) 链路层不抛异常（week 有值 / 无值两条路径）----
  let payloadWeek = null
  let payloadNoWeek = null
  let threw = null
  try {
    payloadWeek = await flow.runCourseQuery(3)
    payloadNoWeek = await flow.runCourseQuery(undefined)
  } catch (err) {
    threw = err
  }
  check('runCourseQuery does NOT throw (week=3)', threw === null, threw ? String(threw?.message ?? threw) : '')
  check('runCourseQuery does NOT throw (week=undefined)', payloadNoWeek !== null)
  check('runCourseQuery(3) returned a payload', payloadWeek !== null && typeof payloadWeek === 'object', JSON.stringify(payloadWeek))
  check('payload role is assistant', payloadWeek?.role === 'assistant', String(payloadWeek?.role))
  check('payload is flagged as error', payloadWeek?.error === true, `error=${payloadWeek?.error}`)

  const content = String(payloadWeek?.content ?? '')
  check('content carries the unified failure prefix', content.includes('课表查询失败'), content.slice(0, 120))
  check('content says the backend is unreachable', content.includes('无法连接后端服务'), content.slice(0, 160))

  // ---- 2) 绝不编造数据 ----
  const FORBIDDEN = [
    '数据结构', '计算机网络', '高等数学', '大学英语', '操作系统', 'Java程序设计', '数据库原理',
    '李明', '王芳', '张伟', '刘洋', '陈静', '孙浩', '赵磊',
    'A302', 'B201', 'C105', 'D202', 'A401', 'E101', 'B305',
    '周一', '周二', '周三', '周四', '周五',
    '08:00-09:40', '10:00-11:40', '14:00-15:40',
    '1-16周', '1-18周', '2-16周', '5-16周',
    '第 3 周',
  ]
  const leaked = FORBIDDEN.filter((k) => content.includes(k))
  check('NO fabricated course data in the failure text', leaked.length === 0, `leaked=${JSON.stringify(leaked)}`)

  // ---- 3) 「这周没有课」的正常回复不能被误用 ----
  check(
    'failure is NOT rendered as "no courses" (empty-list reply)',
    !content.includes('没有课程') && !content.includes('暂无课程记录'),
    content.slice(0, 120),
  )

  // ---- 4) 渲染判定 + 转成 store 消息 ----
  check('shouldRenderReply(payload) is true', flow.shouldRenderReply(payloadWeek) === true)
  const message = flow.toAssistantMessage(payloadWeek)
  check('toAssistantMessage keeps role/content', message?.role === 'assistant' && message?.content === content)
  check('toAssistantMessage propagates the error flag', message?.error === true, `error=${message?.error}`)
  check('toAssistantMessage never marks it as mock', message?.mock !== true, `mock=${message?.mock}`)

  // ---- 5) 错误类型仍然可判别（将来换实现也不破坏调用方）----
  let caught = null
  try {
    await coursesApi.fetchCourseSchedules({ studentId: 'student-001', week: 3 })
  } catch (err) {
    caught = err
  }
  check('fetchCourseSchedules rejects with an Error subclass', caught instanceof Error, caught?.name ?? 'none')
  check('error name is CourseApiError', caught?.name === 'CourseApiError', String(caught?.name))
  check('error kind is network', caught?.kind === 'network', String(caught?.kind))

  // ---- 6) store 能收下这条消息且不会变成占位回复 ----
  // Pinia 在组件外使用需要先装一个 active instance（官方要求，不是本项目的坑）
  const piniaMod = await import(
    pathToFileURL('D:/code/campus-flow/campus-web/node_modules/pinia/dist/pinia.js').href
  )
  piniaMod.setActivePinia(piniaMod.createPinia())

  const store = storeMod.useConversationStore()
  store.appendMessage(message)
  const last = store.messages[store.messages.length - 1]
  check('store accepted the failure message', last?.content === content)
  check('stored message keeps the error flag', last?.error === true)
  check('stored message is NOT a mock reply', last?.mock !== true)
  check('stored failure text has no fabricated data',
    !FORBIDDEN.some((k) => String(last?.content ?? '').includes(k)))
} finally {
  await server.close()
}

const failed = report.filter((r) => !r.ok)
console.log(`\n=== course failure-path: ${report.length - failed.length}/${report.length} passed ===\n`)
process.exitCode = failed.length === 0 ? 0 : 1
