/**
 * F4-1 成绩 Agent 的失败路径验证（真实模块 + 真实 HTTP 桩服务，无浏览器）。
 *
 * 与 `verify-f3-score-failure-path.mjs` 同款姿势（Vite SSR 跑真实 `src/` 模块），
 * 但 F4 的失败面更宽：Agent 是**新引入的一跳**，它自己就是失败来源，
 * 而「失败必须可区分」是 F4-1 的硬要求。因此本脚本用**桩服务**逐个造出四类失败，
 * 断言 `runAgentScoreChat` 归出的 `failureKind` 与文案都对得上：
 *
 *   1. network  —— 端口没人监听（Agent 服务不可用）
 *   2. http     —— 服务返回非 2xx（带 `detail` / 不带 JSON 体两种）
 *   3. malformed——200 但不是合法 JSON / 是 JSON 但字段对不上契约
 *   4. timeout  —— 连上了但永不回包（真实等待 REQUEST_TIMEOUT_MS）
 *
 * 另外断言（这些比「有错误提示」更重要）：
 *   - 失败**永不抛异常**（调用方不必 try/catch）；
 *   - 失败载荷**带 error 标记**（不会被当成正常回答渲染）；
 *   - 失败文案里**没有任何编造的成绩数据**；
 *   - 失败时 threadId 不被伪造（已有会话标识原样保留）；
 *   - **回退目标仍然健康**：同一时刻 F3 直连链路能真实查到成绩——
 *     这正是「Agent 失败后回退 F3」在工程上成立的前提，必须一起验证。
 *
 * 用法：node scripts/verify-f4-agent-score-failure-path.mjs
 */

import { createServer as createHttpServer } from 'node:http'
import { createServer } from 'vite'

const UNREACHABLE = 'http://127.0.0.1:59999'
const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080'
const STUDENT_ID = process.env.STUDENT_ID ?? 'student-001'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}

const srcRoot = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

let ssrServer = null
async function createSsrServer(baseUrl) {  return createServer({
    root: process.cwd(),
    logLevel: 'error',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false },
    resolve: { alias: [{ find: '@', replacement: srcRoot }] },
    // 关键：模块级常量在被导入时就指向目标地址
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify(baseUrl) },
  })
}

// ---------------------------------------------------------------------------
// 桩服务：按 stubMode 返回不同的「坏响应」
// ---------------------------------------------------------------------------
const STUB_MALFORMED_JSON = 'malformed-json'
const STUB_MALFORMED_SHAPE = 'malformed-shape'
const STUB_HTTP_DETAIL = 'http-detail'
const STUB_HTTP_PLAIN = 'http-plain'
const STUB_HANG = 'hang'

let stubMode = STUB_MALFORMED_JSON
const hangingSockets = new Set()

const stubServer = createHttpServer((req, res) => {
  const payload = { mode: stubMode }
  if (stubMode === STUB_HANG) {
    hangingSockets.add(req.socket)
    req.socket.on('close', () => hangingSockets.delete(req.socket))
    return // 永不回包，等客户端超时
  }
  res.setHeader('Content-Type', 'application/json')
  if (stubMode === STUB_MALFORMED_JSON) {
    res.statusCode = 200
    res.end('this is definitely not json')
    return
  }
  if (stubMode === STUB_MALFORMED_SHAPE) {
    res.statusCode = 200
    res.end(JSON.stringify({ reply: 1, intent: null, toolCalls: 'nope' }))
    return
  }
  if (stubMode === STUB_HTTP_DETAIL) {
    res.statusCode = 500
    res.end(JSON.stringify({ error: 'internal_error', detail: '桩服务：Agent 处理请求时发生内部错误。' }))
    return
  }
  if (stubMode === STUB_HTTP_PLAIN) {
    res.statusCode = 503
    res.end('<html>service unavailable</html>')
    return
  }
  res.statusCode = 500
  res.end(JSON.stringify(payload))
})

await new Promise((resolve) => stubServer.listen(0, '127.0.0.1', resolve))
const stubPort = stubServer.address().port
const STUB_BASE = `http://127.0.0.1:${stubPort}`

const unreachableServer = await createSsrServer(UNREACHABLE)
const stubSsrServer = await createSsrServer(STUB_BASE)
const bootServer = await createSsrServer(BASE_URL)

/**
 * 「编造的成绩数据」的判定用**形态**而不是裸课程名：
 * 「返回数据结构与约定不一致」里也含「数据结构」四个字，用裸词表会误报
 * （这个坑在第一次跑时就踩到了）。真正要禁的是「课程名 + 分数」这类成对出现的数据。
 */
const FABRICATION_PATTERNS = [
  /成绩[:：]\s*\d/,
  /学分[:：]\s*\d/,
  /操作系统[^\n]{0,8}\d/,
  /数据结构[^\n]{0,8}\d/,
  /计算机网络[^\n]{0,8}\d/,
  /高等数学|大学英语|大学物理|线性代数/,
  /20\d{2}-20\d{2}-[12]\s*学期成绩/,
]
const leaks = (text) =>
  FABRICATION_PATTERNS.filter((pattern) => pattern.test(String(text))).map((p) => String(p))

try {
  console.log('=== F4-1 score agent failure-path verification ===')
  console.log(`unreachable=${UNREACHABLE} stub=${STUB_BASE} SpringBoot=${BASE_URL}\n`)

  // -------------------------------------------------------------------------
  // 1) network：Agent 服务不可用
  // -------------------------------------------------------------------------
  console.log('--- 1) network: no listener on the Agent port ---')
  const env = await unreachableServer.ssrLoadModule('/src/config/env.ts')
  const agentApi = await unreachableServer.ssrLoadModule('/src/api/agent.ts')
  const flow = await unreachableServer.ssrLoadModule('/src/lib/agentScoreFlow.ts')

  check('base URL points at the unreachable port', env.API_BASE_URL === UNREACHABLE, env.API_BASE_URL)
  check('checkAgentHealth() degrades to false (never throws)', (await agentApi.checkAgentHealth()) === false)

  let networkTurn = null
  let threw = null
  try {
    networkTurn = await flow.runAgentScoreChat('帮我查一下成绩')
  } catch (error) {
    threw = error
  }
  check('runAgentScoreChat does NOT throw when the Agent is down', threw === null,
    threw ? String(threw?.message ?? threw) : '')
  check('failure is flagged with error=true', networkTurn?.reply?.error === true, `error=${networkTurn?.reply?.error}`)
  check('failureKind is "network" (service unavailable is distinguished)',
    networkTurn?.failureKind === 'network', String(networkTurn?.failureKind))
  check('the message says the Agent service is unreachable',
    String(networkTurn?.reply?.content ?? '').includes('无法连接 Agent 服务'),
    String(networkTurn?.reply?.content ?? '').slice(0, 120))
  check('the failed round returns NO threadId (nothing to remember)',
    networkTurn?.threadId === '', JSON.stringify(networkTurn?.threadId))
  check('failed payload IS renderable (the user must see the failure)',
    flow.shouldRenderAgentReply(networkTurn.reply) === true)
  check('failed payload carries the error flag into the store message',
    flow.toAgentAssistantMessage(networkTurn.reply).error === true)
  check('NO fabricated score data in the failure text', leaks(networkTurn.reply.content).length === 0,
    `leaked=${JSON.stringify(leaks(networkTurn.reply.content))}`)

  const keptThread = await flow.runAgentScoreChat('帮我查一下成绩', 'thread-keep-001')
  check('an existing threadId is preserved on failure (retry stays in the same conversation)',
    keptThread.threadId === 'thread-keep-001', keptThread.threadId)

  let caught = null
  try {
    await agentApi.sendAgentChat('帮我查一下成绩')
  } catch (error) {
    caught = error
  }
  check('sendAgentChat still rejects with AgentApiError', caught?.name === 'AgentApiError', String(caught?.name))
  check('AgentApiError carries kind=network', caught?.kind === 'network', String(caught?.kind))

  // -------------------------------------------------------------------------
  // 2) 回退目标健康：同一时刻 F3 直连链路能真实查到成绩
  // -------------------------------------------------------------------------
  console.log('\n--- 2) the F3 fallback target is healthy at the same moment ---')
  const scoreFlow = await bootServer.ssrLoadModule('/src/lib/scoreFlow.ts')
  let fallbackPayload = null
  try {
    fallbackPayload = await scoreFlow.runScoreQuery('2025-2026-1')
  } catch (error) {
    check('F3 fallback chain does not throw', false, String(error?.message ?? error))
  }
  check('the F3 fallback REALLY returns scores while the Agent is down',
    String(fallbackPayload?.content ?? '').includes('操作系统') &&
      String(fallbackPayload?.content ?? '').includes('成绩：95'),
    String(fallbackPayload?.content ?? '').slice(0, 100))
  check('the F3 fallback payload is not an error payload', fallbackPayload?.error !== true)
  check('the F3 fallback carries no Agent failure wording',
    !String(fallbackPayload?.content ?? '').includes('智能助手暂时无法回答'))

  // -------------------------------------------------------------------------
  // 3) http / malformed / timeout：桩服务逐个造
  // -------------------------------------------------------------------------
  const stubAgentApi = await stubSsrServer.ssrLoadModule('/src/api/agent.ts')
  const stubFlow = await stubSsrServer.ssrLoadModule('/src/lib/agentScoreFlow.ts')
  console.log(`\n--- 3) stub-driven failures at ${STUB_BASE} ---`)

  stubMode = STUB_MALFORMED_JSON
  const badJson = await stubFlow.runAgentScoreChat('帮我查一下成绩')
  check('200 + non-JSON body -> failureKind "malformed"', badJson.failureKind === 'malformed', String(badJson.failureKind))
  check('200 + non-JSON body -> the text says it is not valid JSON',
    String(badJson.reply.content).includes('不是合法 JSON'), String(badJson.reply.content).slice(0, 100))

  stubMode = STUB_MALFORMED_SHAPE
  const badShape = await stubFlow.runAgentScoreChat('帮我查一下成绩')
  check('200 + JSON with the wrong shape -> failureKind "malformed"', badShape.failureKind === 'malformed', String(badShape.failureKind))
  check('200 + wrong shape -> the text says the structure does not match',
    String(badShape.reply.content).includes('数据结构与约定不一致'), String(badShape.reply.content).slice(0, 100))

  stubMode = STUB_HTTP_DETAIL
  const httpDetail = await stubFlow.runAgentScoreChat('帮我查一下成绩')
  check('500 + {detail} -> failureKind "http"', httpDetail.failureKind === 'http', String(httpDetail.failureKind))
  check('500 + {detail} -> the server detail is surfaced to the user',
    String(httpDetail.reply.content).includes('桩服务：Agent 处理请求时发生内部错误'),
    String(httpDetail.reply.content).slice(0, 100))

  let httpError = null
  try {
    await stubAgentApi.sendAgentChat('帮我查一下成绩')
  } catch (error) {
    httpError = error
  }
  check('AgentApiError.status carries the HTTP status', httpError?.status === 500, String(httpError?.status))

  stubMode = STUB_HTTP_PLAIN
  const httpPlain = await stubFlow.runAgentScoreChat('帮我查一下成绩')
  check('503 without a JSON body -> failureKind "http"', httpPlain.failureKind === 'http', String(httpPlain.failureKind))
  check('503 without a JSON body -> falls back to the status code in the text',
    String(httpPlain.reply.content).includes('HTTP 503'), String(httpPlain.reply.content).slice(0, 120))

  stubMode = STUB_HANG
  const startedAt = Date.now()
  const timedOut = await stubFlow.runAgentScoreChat('帮我查一下成绩')
  const elapsed = Date.now() - startedAt
  check('a hanging server -> failureKind "timeout"', timedOut.failureKind === 'timeout', String(timedOut.failureKind))
  check('timeout is produced by the client-side deadline, not by the server',
    elapsed >= 14000 && elapsed <= 25000, `elapsed=${elapsed}ms`)
  check('timeout text tells the user to check the Agent service',
    String(timedOut.reply.content).includes('请求超时'), String(timedOut.reply.content).slice(0, 120))

  // -------------------------------------------------------------------------
  // 4) 所有失败面共同的不变量
  // -------------------------------------------------------------------------
  console.log('\n--- 4) shared invariants across every failure kind ---')
  const everyFailure = [
    ['network', networkTurn],
    ['malformed(json)', badJson],
    ['malformed(shape)', badShape],
    ['http(detail)', httpDetail],
    ['http(plain)', httpPlain],
    ['timeout', timedOut],
  ]
  check('every failure kind was produced (4 distinct kinds covered)',
    new Set(everyFailure.map(([, turn]) => turn.failureKind)).size === 4,
    JSON.stringify([...new Set(everyFailure.map(([, turn]) => turn.failureKind))]))
  check('every failure is flagged error=true (never rendered as success)',
    everyFailure.every(([, turn]) => turn.reply.error === true))
  check('every failure carries a non-empty, user-facing message',
    everyFailure.every(([, turn]) => String(turn.reply.content).trim().length > 0))
  check('NO failure text ever contains fabricated score data',
    everyFailure.every(([, turn]) => leaks(turn.reply.content).length === 0),
    JSON.stringify(everyFailure.map(([k, turn]) => [k, leaks(turn.reply.content)])))
  check('NO failure text is mistaken for a normal "no scores" answer',
    everyFailure.every(([, turn]) =>
      !String(turn.reply.content).includes('该学期暂无成绩记录') &&
      !String(turn.reply.content).includes('全部学期成绩，共')),
  )

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== score agent failure-path: ${results.length - failed.length}/${results.length} passed ===\n`)
  process.exitCode = failed.length === 0 ? 0 : 1
} finally {
  for (const socket of hangingSockets) socket.destroy()
  stubServer.closeAllConnections?.()
  await new Promise((resolve) => stubServer.close(resolve))
  await unreachableServer.close()
  await stubSsrServer.close()
  await bootServer.close()
}
