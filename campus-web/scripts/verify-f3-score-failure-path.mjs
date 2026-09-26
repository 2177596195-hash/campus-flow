/**
 * F3-3 失败路径验证（真实模块 + 真实 HTTP，无浏览器）。
 *
 * 与 `verify-f3-failure-path.mjs` / `verify-f3-course-failure-path.mjs`
 * 同款姿势：用 Vite 的 SSR 能力即时转译 `src/**` 的真实 TS 模块，
 * 把 `buildApiUrl` 指向一个**确定不可达**的地址，然后断言：
 *   1) `runScoreQuery` **不抛异常**（链路层负责把错误变成一句话），
 *      无论 semester 是明确学期还是 undefined（全部）；
 *   2) 返回的回复**带 `error` 标记**，且文案含统一失败前缀「成绩查询失败」；
 *   3) 回复里**不含任何编造的成绩数据**（课程 / 分数 / 学分一个都不能出现）；
 *   4) 失败不会被误渲染成「该学期暂无成绩」的正常回复；
 *   5) `toAssistantMessage` 产出的消息可直接进 store，不回退成 mock 回复。
 *
 * 用法：node scripts/verify-f3-score-failure-path.mjs
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
  const flow = await load('/src/lib/scoreFlow.ts')
  const scoresApi = await load('/src/api/scores.ts')
  const storeMod = await load('/src/stores/conversation.ts')

  console.log(`API_BASE_URL = ${JSON.stringify(env.API_BASE_URL)}`)
  check('base URL points at the unreachable port', env.API_BASE_URL === UNREACHABLE, env.API_BASE_URL)
  check(
    'built URL targets the unreachable port',
    env.buildApiUrl('/api/scores?studentId=student-001&semester=2025-2026-1').startsWith(UNREACHABLE),
    env.buildApiUrl('/api/scores?studentId=student-001&semester=2025-2026-1'),
  )

  // ---- 1) 链路层不抛异常（明确学期 / 全部学期两条路径）----
  let payloadSemester = null
  let payloadAll = null
  let threw = null
  try {
    payloadSemester = await flow.runScoreQuery('2025-2026-1')
    payloadAll = await flow.runScoreQuery(undefined)
  } catch (err) {
    threw = err
  }
  check('runScoreQuery does NOT throw (semester="2025-2026-1")', threw === null, threw ? String(threw?.message ?? threw) : '')
  check('runScoreQuery does NOT throw (semester=undefined)', payloadAll !== null)
  check('runScoreQuery("2025-2026-1") returned a payload', payloadSemester !== null && typeof payloadSemester === 'object', JSON.stringify(payloadSemester))
  check('payload role is assistant', payloadSemester?.role === 'assistant', String(payloadSemester?.role))
  check('payload is flagged as error', payloadSemester?.error === true, `error=${payloadSemester?.error}`)

  const content = String(payloadSemester?.content ?? '')
  check('content carries the unified failure prefix', content.includes('成绩查询失败'), content.slice(0, 120))
  check('content says the backend is unreachable', content.includes('无法连接后端服务'), content.slice(0, 160))

  // ---- 2) 绝不编造数据 ----
  const FORBIDDEN = [
    '数据结构', '计算机网络', '操作系统', '高等数学', '大学英语', '大学物理', '线性代数',
    '成绩：92', '成绩：88', '成绩：95', '成绩：84', '成绩：86', '成绩：90',
    '学分：4', '学分：3.5', '学分：3', '学分：5', '学分：2',
    '2025-2026-1 学期成绩', '全部学期成绩，共',
  ]
  const leaked = FORBIDDEN.filter((k) => content.includes(k))
  check('NO fabricated score data in the failure text', leaked.length === 0, `leaked=${JSON.stringify(leaked)}`)

  // ---- 3) 失败不会被误渲染成「暂无成绩」的正常回复 ----
  check(
    'failure is NOT rendered as "no scores" (empty-list reply)',
    !content.includes('该学期暂无成绩记录') && !content.includes('暂无任何成绩记录'),
    content.slice(0, 120),
  )

  // ---- 4) 渲染判定 + 转成 store 消息 ----
  check('shouldRenderReply(payload) is true', flow.shouldRenderReply(payloadSemester) === true)
  const message = flow.toAssistantMessage(payloadSemester)
  check('toAssistantMessage keeps role/content', message?.role === 'assistant' && message?.content === content)
  check('toAssistantMessage propagates the error flag', message?.error === true, `error=${message?.error}`)
  check('toAssistantMessage never marks it as mock', message?.mock !== true, `mock=${message?.mock}`)

  // ---- 5) 错误类型仍然可判别（将来换实现也不破坏调用方）----
  let caught = null
  try {
    await scoresApi.fetchScoreRecords({ studentId: 'student-001', semester: '2025-2026-1' })
  } catch (err) {
    caught = err
  }
  check('fetchScoreRecords rejects with an Error subclass', caught instanceof Error, caught?.name ?? 'none')
  check('error name is ScoreApiError', caught?.name === 'ScoreApiError', String(caught?.name))
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
console.log(`\n=== score failure-path: ${report.length - failed.length}/${report.length} passed ===\n`)
process.exitCode = failed.length === 0 ? 0 : 1
