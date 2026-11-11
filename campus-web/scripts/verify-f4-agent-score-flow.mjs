/**
 * F4-1 成绩 Agent 链路验证（真实模块 + 真实 HTTP，无浏览器）。
 *
 * 姿势与 `verify-f3-score-flow.mjs` 一致：用 Vite 的 SSR 能力跑**真实的 `src/`
 * 模块**（不是复制品），通过 `fetch` 打真实 HTTP。差别是本阶段有**两个后端**：
 *
 *   - Python Agent（FastAPI，默认 8000）：`/api/agent/health`、`/api/agent/chat`
 *   - Spring Boot（默认 8080，本机实际跑在 8081）：`/api/scores`（F3 直连链路）
 *
 * 因此脚本起两个 SSR 服务实例，各自把 `VITE_API_BASE_URL` 指向对应的后端
 * （`env.ts` 的模块级常量在导入时就固化，不能一个实例跑两个地址）。
 *
 * 覆盖：
 *   1. Agent 健康检查与 HTTP 契约（字段名 / 类型）；
 *   2. 单轮「帮我查一下成绩」→ Agent 追问学期（不猜学期）；
 *   3. 多轮：同一 threadId 补「2025-2026第一学期」→ 命中 get_score → 真实成绩；
 *   4. 单轮直给学期；
 *   5. 模块层 `runAgentScoreChat` 的 threadId 传递与复用（第一轮不传）；
 *   6. 空成绩（不存在的学期）不产出编造数据；
 *   7. F3 直连链路 + 意图分流回归（考试 / 课程 / 请假不被 Agent 抢走）。
 *
 * 用法（不能带 configFile，SSR 下会触发 dep-scan 报错）：
 *   node scripts/verify-f4-agent-score-flow.mjs
 *   BASE_URL=http://localhost:8081 node scripts/verify-f4-agent-score-flow.mjs
 */

import { createServer } from 'vite'

const AGENT_BASE_URL = process.env.AGENT_BASE_URL ?? 'http://127.0.0.1:8000'
const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080'
const STUDENT_ID = process.env.STUDENT_ID ?? 'student-001'

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}

const srcRoot = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

/** 建一个把 `VITE_API_BASE_URL` 固定成 `baseUrl` 的 SSR 服务器 */
async function createSsrServer(baseUrl) {
  process.env.VITE_CAMPUS_STUDENT_ID = STUDENT_ID
  return createServer({
    root: process.cwd(),
    logLevel: 'error',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false },
    resolve: { alias: [{ find: '@', replacement: srcRoot }] },
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify(baseUrl) },
  })
}

/** 直接打 Agent 的原始 HTTP 接口（不经前端模块），用于核对契约本身 */
async function agentPost(message, threadId) {
  const body = { message }
  if (threadId) body.threadId = threadId
  const response = await fetch(`${AGENT_BASE_URL}/api/agent/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  })
  const text = await response.text()
  let payload = null
  try {
    payload = JSON.parse(text)
  } catch {
    payload = null
  }
  return { status: response.status, payload, raw: text.slice(0, 200) }
}

const agentServer = await createSsrServer(AGENT_BASE_URL)
const bootServer = await createSsrServer(BASE_URL)

try {
  console.log('=== F4-1 score agent chain verification ===')
  console.log(`Agent=${AGENT_BASE_URL} SpringBoot=${BASE_URL} StudentId=${STUDENT_ID}\n`)

  // -------------------------------------------------------------------------
  // 1) 健康检查
  // -------------------------------------------------------------------------
  console.log('--- 1) Agent health ---')
  let health = null
  try {
    const response = await fetch(`${AGENT_BASE_URL}/api/agent/health`, {
      headers: { Accept: 'application/json' },
    })
    health = { status: response.status, body: await response.json() }
  } catch (error) {
    health = { status: 'FETCH-ERROR', body: null, error: String(error?.message ?? error) }
  }
  check('GET /api/agent/health -> 200', health.status === 200, `status=${health.status}`)
  check('health body is {status:"ok"}', health.body?.status === 'ok', JSON.stringify(health.body))

  const agentModule = await agentServer.ssrLoadModule('/src/api/agent.ts')
  const flowModule = await agentServer.ssrLoadModule('/src/lib/agentScoreFlow.ts')
  check('checkAgentHealth() reports the service is up', (await agentModule.checkAgentHealth()) === true)

  // -------------------------------------------------------------------------
  // 2) 单轮：不猜学期
  // -------------------------------------------------------------------------
  console.log('\n--- 2) raw POST /api/agent/chat (no semester) ---')
  const turn1 = await agentPost('帮我查一下成绩')
  console.log(`reply: ${String(turn1.payload?.reply ?? turn1.raw).slice(0, 120)}`)
  check('POST /api/agent/chat -> 200', turn1.status === 200, `status=${turn1.status}`)
  check('response carries the four contract fields',
    turn1.payload !== null &&
      typeof turn1.payload.reply === 'string' &&
      typeof turn1.payload.intent === 'string' &&
      Array.isArray(turn1.payload.toolCalls) &&
      typeof turn1.payload.threadId === 'string',
    JSON.stringify(Object.keys(turn1.payload ?? {})))
  check('threadId is generated and non-empty', (turn1.payload?.threadId ?? '').length > 0, turn1.payload?.threadId)
  check('intent is "query" for a score question', turn1.payload?.intent === 'query', String(turn1.payload?.intent))
  check('no tool was called yet (the Agent asks back instead of guessing)',
    Array.isArray(turn1.payload?.toolCalls) && turn1.payload.toolCalls.length === 0,
    JSON.stringify(turn1.payload?.toolCalls))
  check('the Agent asks for the semester (does not invent one)',
    /学期/.test(String(turn1.payload?.reply)) && !/操作系统|数据结构|计算机网络|9[0-9]/.test(String(turn1.payload?.reply)),
    String(turn1.payload?.reply).slice(0, 80))
  check('no fabricated scores in the follow-up question',
    !/\d{2}\s*分|成绩[:：]\s*\d/.test(String(turn1.payload?.reply)))

  // -------------------------------------------------------------------------
  // 3) 多轮：同一个 threadId 补学期 -> 真实 tool 调用
  // -------------------------------------------------------------------------
  console.log('\n--- 3) multi-turn with the same threadId ---')
  const threadId = turn1.payload?.threadId
  const turn2 = await agentPost('2025-2026第一学期', threadId)
  console.log(`reply: ${String(turn2.payload?.reply ?? '').replace(/\n/g, ' ').slice(0, 200)}`)
  check('round 2 -> 200', turn2.status === 200, `status=${turn2.status}`)
  check('round 2 echoes back the SAME threadId', turn2.payload?.threadId === threadId,
    `${turn2.payload?.threadId} vs ${threadId}`)
  check('round 2 reaches the score tool', (turn2.payload?.toolCalls ?? []).includes('get_score'),
    JSON.stringify(turn2.payload?.toolCalls))
  const turn2Reply = String(turn2.payload?.reply ?? '')
  check('round 2 answers with the REAL rows (操作系统 95 / 数据结构 92 / 计算机网络 88)',
    turn2Reply.includes('操作系统') && turn2Reply.includes('95') &&
      turn2Reply.includes('数据结构') && turn2Reply.includes('92') &&
      turn2Reply.includes('计算机网络') && turn2Reply.includes('88'),
    turn2Reply.replace(/\n/g, ' ').slice(0, 160))
  check('round 2 does NOT ask for the semester again', !/请问你要查询哪个学期/.test(turn2Reply))

  // -------------------------------------------------------------------------
  // 4) 单轮直接带学期
  // -------------------------------------------------------------------------
  console.log('\n--- 4) raw single turn with an explicit semester ---')
  const turn3 = await agentPost('帮我查一下2025-2026-1的成绩')
  console.log(`reply: ${String(turn3.payload?.reply ?? '').replace(/\n/g, ' ').slice(0, 200)}`)
  check('single turn with semester -> 200', turn3.status === 200, `status=${turn3.status}`)
  check('single turn calls get_score in one round', (turn3.payload?.toolCalls ?? []).includes('get_score'),
    JSON.stringify(turn3.payload?.toolCalls))
  check('single turn returns the real rows',
    /操作系统/.test(String(turn3.payload?.reply)) && /数据结构/.test(String(turn3.payload?.reply)),
    String(turn3.payload?.reply).replace(/\n/g, ' ').slice(0, 120))
  check('a brand-new threadId is issued for a brand-new conversation',
    typeof turn3.payload?.threadId === 'string' && turn3.payload.threadId !== threadId,
    `${turn3.payload?.threadId} vs ${threadId}`)

  // -------------------------------------------------------------------------
  // 5) 空成绩：不编造
  // -------------------------------------------------------------------------
  console.log('\n--- 5) empty score set (nonexistent semester) ---')
  const turn4 = await agentPost('帮我查一下2026-2027-1的成绩')
  const turn4Reply = String(turn4.payload?.reply ?? '')
  console.log(`reply: ${turn4Reply.replace(/\n/g, ' ').slice(0, 200)}`)
  check('nonexistent semester -> 200 (not an error)', turn4.status === 200, `status=${turn4.status}`)
  check('the tool was really called for the empty case', (turn4.payload?.toolCalls ?? []).includes('get_score'),
    JSON.stringify(turn4.payload?.toolCalls))
  check('empty result is reported as "no records" and NOT fabricated',
    !/操作系统|数据结构|计算机网络/.test(turn4Reply) &&
      /暂无|没有|未查询到|无记录|没有找到/.test(turn4Reply),
    turn4Reply.replace(/\n/g, ' ').slice(0, 140))

  // -------------------------------------------------------------------------
  // 6) 模块层：runAgentScoreChat 的 threadId 生命周期
  // -------------------------------------------------------------------------
  console.log('\n--- 6) module layer: runAgentScoreChat ---')
  const moduleTurn1 = await flowModule.runAgentScoreChat('帮我查一下成绩')
  check('module round 1 does not throw and is not an error', moduleTurn1.reply.error !== true,
    moduleTurn1.reply.content.slice(0, 80))
  check('module round 1 returns a threadId', moduleTurn1.threadId.length > 0, moduleTurn1.threadId)
  check('module round 1 has no failureKind', moduleTurn1.failureKind === undefined,
    String(moduleTurn1.failureKind))
  check('module round 1 payload is renderable', flowModule.shouldRenderAgentReply(moduleTurn1.reply) === true)

  const moduleTurn2 = await flowModule.runAgentScoreChat('2025-2026第一学期', moduleTurn1.threadId)
  check('module round 2 keeps the same threadId', moduleTurn2.threadId === moduleTurn1.threadId,
    `${moduleTurn2.threadId} vs ${moduleTurn1.threadId}`)
  check('module round 2 is not an error', moduleTurn2.reply.error !== true, moduleTurn2.reply.content.slice(0, 80))
  check('module round 2 result renders the real scores',
    moduleTurn2.reply.content.includes('操作系统') && moduleTurn2.reply.content.includes('95'),
    moduleTurn2.reply.content.replace(/\n/g, ' ').slice(0, 120))
  const moduleMessage = flowModule.toAgentAssistantMessage(moduleTurn2.reply)
  check('toAgentAssistantMessage maps role/content without the error flag',
    moduleMessage.role === 'assistant' && moduleMessage.content === moduleTurn2.reply.content && moduleMessage.error !== true)

  const independent = await flowModule.runAgentScoreChat('帮我查一下2025-2026-1的成绩')
  check('calling again without a threadId gets a fresh conversation (no shared global state)',
    independent.threadId !== moduleTurn1.threadId, `${independent.threadId} vs ${moduleTurn1.threadId}`)

  // -------------------------------------------------------------------------
  // 7) F3 回归：直连 Spring Boot 链路仍然可用
  // -------------------------------------------------------------------------
  console.log('\n--- 7) F3 regression (direct Spring Boot chain) ---')
  const scoreFlow = await bootServer.ssrLoadModule('/src/lib/scoreFlow.ts')
  const scoresApi = await bootServer.ssrLoadModule('/src/api/scores.ts')
  const intent = await bootServer.ssrLoadModule('/src/constants/scoreIntent.ts')
  const examIntent = await bootServer.ssrLoadModule('/src/constants/examIntent.ts')
  const courseIntent = await bootServer.ssrLoadModule('/src/constants/courseIntent.ts')
  const leaveIntent = await bootServer.ssrLoadModule('/src/constants/leaveIntent.ts')

  let f3Semester = null
  let f3Empty = null
  try {
    f3Semester = await scoreFlow.runScoreQuery('2025-2026-1')
    f3Empty = await scoreFlow.runScoreQuery('2026-2027-1')
  } catch (error) {
    check('F3 runScoreQuery does not throw', false, String(error?.message ?? error))
  }
  check('F3 direct chain still returns the real rows',
    String(f3Semester?.content ?? '').includes('操作系统') &&
      String(f3Semester?.content ?? '').includes('成绩：95'),
    String(f3Semester?.content ?? '').slice(0, 100))
  check('F3 empty result is still a normal answer', String(f3Empty?.content ?? '').includes('该学期暂无成绩记录'),
    String(f3Empty?.content ?? '').slice(0, 80))
  check('F3 payload has no error flag', f3Semester?.error !== true)

  const rawScores = await scoresApi.fetchScoreRecords({ studentId: STUDENT_ID, semester: '2025-2026-1' })
  check('F3 API module returns 3 rows with numeric score/credit',
    rawScores.length === 3 && rawScores.every((x) => typeof x.score === 'number' && typeof x.credit === 'number'),
    `count=${rawScores.length}`)
  const noStudent = await scoresApi.fetchScoreRecords({ studentId: 'student-999', semester: '2025-2026-1' })
  check('nonexistent student still returns an empty array (200 + [])', Array.isArray(noStudent) && noStudent.length === 0)

  // 意图分流：只有成绩问题会被送到 Agent，考试 / 课程 / 请假仍走各自链路
  check('score questions still hit the score intent', intent.isScoreQuery('帮我查一下成绩'))
  check('exam / course / leave questions do NOT hit the score intent',
    !intent.isScoreQuery('帮我查一下考试安排') &&
      !intent.isScoreQuery('查看第3周课程') &&
      !intent.isScoreQuery('我想请事假'))
  check('exam / course / leave intents are intact',
    examIntent.isExamQuery('帮我查一下考试安排') &&
      courseIntent.isCourseQuery('查看第3周课程') &&
      leaveIntent.isLeaveQuery('我想请事假'))

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== summary: ${results.length - failed.length}/${results.length} passed ===`)
  process.exitCode = failed.length === 0 ? 0 : 1
} finally {
  await agentServer.close()
  await bootServer.close()
}
