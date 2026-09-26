/**
 * F3-4 请假失败路径验证（真实模块 + 不可达 HTTP，无浏览器）。
 *
 * 与 `verify-f3-score-failure-path.mjs` 同款姿势：
 * 把 buildApiUrl 指向一个确定不可达的地址，然后断言：
 *   1) submitLeave 不抛异常；
 *   2) 返回的回复带 error 标记，且文案含统一失败前缀「请假提交失败」；
 *   3) 回复里不含任何编造的请假数据；
 *   4) toAssistantMessage 产出的消息可直接进 store；
 *   5) 错误类型仍可判别。
 *
 * 用法：node scripts/verify-f3-leave-failure-path.mjs
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
  const flow = await load('/src/lib/leaveFlow.ts')
  const leavesApi = await load('/src/api/leaves.ts')
  const storeMod = await load('/src/stores/conversation.ts')

  console.log(`API_BASE_URL = ${JSON.stringify(env.API_BASE_URL)}`)
  check('base URL points at the unreachable port', env.API_BASE_URL === UNREACHABLE, env.API_BASE_URL)

  // ---- 1) 链路层不抛异常 ----
  const completeData = {
    startTime: '2026-10-12 08:00',
    endTime: '2026-10-14 18:00',
    reason: '感冒就医',
  }

  let payload = null
  let threw = null
  try {
    payload = await flow.submitLeave(completeData)
  } catch (err) {
    threw = err
  }
  check('submitLeave does NOT throw', threw === null, threw ? String(threw?.message ?? threw) : '')
  check('submitLeave returned a payload', payload !== null && typeof payload === 'object', JSON.stringify(payload))
  check('payload role is assistant', payload?.role === 'assistant', String(payload?.role))
  check('payload is flagged as error', payload?.error === true, `error=${payload?.error}`)

  const content = String(payload?.content ?? '')
  check('content carries the unified failure prefix', content.includes('请假提交失败'), content.slice(0, 120))
  check('content says the backend is unreachable', content.includes('无法连接后端服务'), content.slice(0, 160))

  // ---- 2) 绝不编造数据 ----
  const FORBIDDEN = [
    '申请编号：1', '申请编号：2', '申请编号：3',
    '学号：student-001',
    '当前状态：PENDING',
    '请假申请已提交成功',
  ]
  const leaked = FORBIDDEN.filter((k) => content.includes(k))
  check('NO fabricated leave data in the failure text', leaked.length === 0, `leaked=${JSON.stringify(leaked)}`)

  // ---- 3) 渲染判定 + 转成 store 消息 ----
  check('shouldRenderReply(payload) is true', flow.shouldRenderReply(payload) === true)
  const message = flow.toAssistantMessage(payload)
  check('toAssistantMessage keeps role/content', message?.role === 'assistant' && message?.content === content)
  check('toAssistantMessage propagates the error flag', message?.error === true, `error=${message?.error}`)
  check('toAssistantMessage never marks it as mock', message?.mock !== true, `mock=${message?.mock}`)

  // ---- 4) 错误类型仍可判别 ----
  let caught = null
  try {
    await leavesApi.submitLeaveApplication({
      studentId: 'student-001',
      startTime: '2026-10-12 08:00',
      endTime: '2026-10-14 18:00',
      reason: 'test',
    })
  } catch (err) {
    caught = err
  }
  check('submitLeaveApplication rejects with an Error subclass', caught instanceof Error, caught?.name ?? 'none')
  check('error name is LeaveApiError', caught?.name === 'LeaveApiError', String(caught?.name))
  check('error kind is network', caught?.kind === 'network', String(caught?.kind))

  // ---- 5) 不完整数据不会提交（不触网）----
  let incompletePayload = null
  try {
    incompletePayload = await flow.submitLeave({})
  } catch (err) {
    // should not throw
  }
  check('submitLeave with empty data does NOT throw', incompletePayload !== null)
  check('submitLeave with empty data returns error', incompletePayload?.error === true)
  check('submitLeave with empty data says incomplete',
    incompletePayload?.content?.includes('不完整') === true,
    incompletePayload?.content?.slice(0, 60))

  // ---- 6) store 能收下这条消息 ----
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
console.log(`\n=== leave failure-path: ${report.length - failed.length}/${report.length} passed ===\n`)
process.exitCode = failed.length === 0 ? 0 : 1
