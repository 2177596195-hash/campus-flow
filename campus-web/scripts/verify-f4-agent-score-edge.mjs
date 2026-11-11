/**
 * F4-1 真实浏览器端到端验证（本机 Edge + CDP）。
 *
 * 为什么必须有这一层：F4 新增的是**浏览器 → Vite 代理 → Python Agent** 这条跨源
 * 边界的全链路。模块级（Node 侧 fetch）验证得再充分，也不能替代浏览器结论——
 * 真正决定成败的是浏览器是否把 `/api/agent/chat` 当作同源请求发出去
 * （这正是 F3-1 用同源相对路径 + Vite 反代绕开 CORS 的那条老结论）。
 * 因此本脚本用独立 user-data-dir 起 headless Edge，Dump 真实的
 * Network 事件（URL / 方法 / **请求体** / 状态码 / 响应体）来定论。
 *
 * 两种模式：
 *   up   （默认）Agent 在线：
 *     1) 「帮我查一下成绩」→ 浏览器发 POST /api/agent/chat（**不带 threadId**）
 *        → Agent 反问学期 → 页面渲染追问、无「失败」标记、不发 /api/scores；
 *     2) 「2025-2026第一学期」→ 第二轮 **带同一个 threadId** 再发 Agent
 *        → 页面渲染真实成绩（操作系统 95 等）；
 *     3) 考试 / 课程问题仍走 F3 原链路（/api/exams、/api/courses），不被 Agent 抢走。
 *   down Agent 在线不可用（由外面把 Agent 停掉后传 down）：
 *     4) 成绩问题 → /api/agent/chat 失败 → **回退 F3** → 浏览器改打
 *        /api/scores → 页面渲染真实成绩，无「失败」标记。
 *
 * 用法：node scripts/verify-f4-agent-score-edge.mjs <pageUrl> <label> [up|down]
 */

import { createRequire } from 'node:module'
import { writeFileSync, mkdirSync } from 'node:fs'
import { spawn } from 'node:child_process'

const require = createRequire('file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/')
const WebSocket = require('ws')

const PAGE_URL = process.argv[2] ?? 'http://localhost:5199/'
const LABEL = process.argv[3] ?? 'run'
const MODE = (process.argv[4] ?? 'up').toLowerCase()
const CDP_PORT = 9234
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const PROFILE = 'D:/code/campus-flow/campus-web/.edge-profile-f4'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

mkdirSync(PROFILE, { recursive: true })
const edge = spawn(
  EDGE,
  [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    'about:blank',
  ],
  { detached: false, stdio: 'ignore' },
)

let version = null
for (let i = 0; i < 40; i++) {
  try {
    version = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).json()
    break
  } catch {
    await sleep(500)
  }
}
if (!version) {
  console.error('CDP not reachable; Edge failed to start')
  edge.kill()
  process.exit(1)
}
console.log(`Edge CDP ready: ${version.Browser} | mode=${MODE}`)

const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
let page = targets.find((t) => t.type === 'page')
if (!page) {
  page = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?about:blank`, { method: 'PUT' })).json()
}

const ws = new WebSocket(page.webSocketDebuggerUrl, { perMessageDeflate: false })
await new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej) })

let nextId = 1
const pending = new Map()
/** 本轮（turn）内的网络事件缓冲；每轮开始前清空 */
let requests = []
let responses = []
let failures = []
const bodies = new Map()
const consoleErrors = []

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString())
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id)
    pending.delete(msg.id)
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
    return
  }
  if (msg.method === 'Network.requestWillBeSent') {
    const { url, method, postData, headers } = msg.params.request
    requests.push({ id: msg.params.requestId, url, method, postData: postData ?? null, contentType: headers?.['Content-Type'] ?? headers?.['content-type'] ?? null })
  }
  if (msg.method === 'Network.responseReceived') {
    const { url, status, headers } = msg.params.response
    responses.push({
      id: msg.params.requestId,
      url,
      status,
      acao: headers?.['access-control-allow-origin'] ?? null,
    })
  }
  if (msg.method === 'Network.loadingFailed') {
    const req = requests.find((r) => r.id === msg.params.requestId)
    failures.push({ id: msg.params.requestId, url: req?.url ?? '(unknown)', err: msg.params.errorText, blocked: msg.params.blockedReason ?? null })
  }
  if (msg.method === 'Network.loadingFinished') {
    // 异步取响应体：Agent 的 threadId 只能从这里读出来
    void (async () => {
      try {
        const body = await send('Network.getResponseBody', { requestId: msg.params.requestId })
        bodies.set(msg.params.requestId, body.body)
      } catch {
        /* 预检 / 重定向等没有可读 body，忽略 */
      }
    })()
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? '').join(' '))
  }
})

function send(method, params = {}) {
  const id = nextId++
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    ws.send(JSON.stringify({ id, method, params }))
  })
}

async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.text + ' :: ' + (r.exceptionDetails.exception?.description ?? ''))
  }
  return r.result.value
}

const report = []
const check = (name, ok, detail = '') => {
  report.push({ name, ok, detail })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}

/** 用原生 setter 写 textarea 并派发 input 事件，让 Vue 的 v-model 真正收到值 */
const SET_DRAFT = (text) => `
  (() => {
    const ta = document.querySelector('textarea');
    if (!ta) return 'NO_TEXTAREA';
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, ${JSON.stringify(text)});
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    return ta.value;
  })()
`

const READ_ASSISTANTS = `
  (() => [...document.querySelectorAll('.msg--assistant')].map(n => ({
    text: n.querySelector('.msg__text')?.textContent ?? '',
    tag: n.querySelector('.msg__tag')?.textContent?.trim() ?? null,
  })))()
`

/**
 * 等真正的回复渲染：必须出现**非空文本**的 assistant 消息。
 * 不能只数节点——「正在生成」占位气泡也是 assistant 节点、内容为空，
 * 会立刻假命中（F3-1 就踩过这个坑）。
 */
async function waitForReply(above, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const list = await evaluate(READ_ASSISTANTS)
    const last = list?.[list.length - 1]
    if (list.length > above && last && String(last.text ?? '').trim().length > 0) return list
    await sleep(400)
  }
  return null
}

async function sendAndWait(text, label) {
  const before = (await evaluate(READ_ASSISTANTS)).length
  const filled = await evaluate(SET_DRAFT(text))
  check(`typed "${text}"`, filled === text, String(filled))
  requests = []
  responses = []
  failures = []
  bodies.clear()
  await evaluate(`(() => { document.querySelector('.composer__send').click(); return true })()`)
  const list = await waitForReply(before)
  check(`reply arrived for #${label} (non-empty text)`,
    Array.isArray(list) && list.length > before, `before=${before} after=${list?.length}`)
  await sleep(800) // 给尾随的 Network / 响应体读取留时间
  const answer = list?.[list.length - 1] ?? {}
  console.log(`\nNETWORK #${label}:\n${JSON.stringify(requests.map((r) => ({ url: r.url, method: r.method, postData: r.postData })), null, 2)}`)
  console.log(`STATUS #${label}: ${JSON.stringify(responses.map((r) => ({ url: r.url, status: r.status, acao: r.acao })))}`)
  console.log(`RENDERED #${label} (tag=${answer.tag}):\n${String(answer.text ?? '').slice(0, 400)}\n`)
  return {
    answer,
    list,
    requests,
    responses,
    failures,
    agentRequestBody: requests.find((r) => r.url.includes('/api/agent/chat'))?.postData ?? null,
    agentResponseBody: (() => {
      const req = requests.find((r) => r.url.includes('/api/agent/chat'))
      return req ? bodies.get(req.id) ?? null : null
    })(),
  }
}

const agentThreadIdOf = (turn) => {
  try {
    return JSON.parse(turn.agentResponseBody ?? '{}').threadId ?? null
  } catch {
    return null
  }
}

await send('Network.enable')
await send('Runtime.enable')
await send('Page.enable')

await send('Page.navigate', { url: PAGE_URL })
await sleep(3500)

const title = await evaluate('document.title')
check('page loaded (title)', String(title).includes('CampusFlow'), String(title))

if (MODE === 'up') {
  // -------------------------------------------------------------------------
  // 第一轮：Agent 反问学期（浏览器必须真的发出 /api/agent/chat，且不带 threadId）
  // -------------------------------------------------------------------------
  const turn1 = await sendAndWait('帮我查一下成绩', '1-agent-followup')
  check('#1 the browser really called the Agent (POST /api/agent/chat)',
    turn1.requests.some((r) => r.url.includes('/api/agent/chat') && r.method === 'POST'),
    JSON.stringify(turn1.requests.map((r) => `${r.method} ${r.url}`)))
  check('#1 the Agent call is SAME-ORIGIN (page host + /api/agent/chat, no CORS preflight AC-AO needed)',
    (() => {
      const req = turn1.requests.find((r) => r.url.includes('/api/agent/chat'))
      return Boolean(req) && new URL(req.url).host === new URL(PAGE_URL).host
    })(),
    turn1.requests.find((r) => r.url.includes('/api/agent/chat'))?.url ?? 'none')
  check('#1 the FIRST Agent call carries NO threadId',
    turn1.agentRequestBody !== null && !/'threadId'/.test(turn1.agentRequestBody),
    String(turn1.agentRequestBody))
  const thread1 = agentThreadIdOf(turn1)
  check('#1 the Agent replied with a threadId', typeof thread1 === 'string' && thread1.length > 0, String(thread1))
  check('#1 the Agent HTTP call succeeded (no request-level failure)',
    turn1.failures.length === 0 || turn1.failures.every((f) => !f.url.includes('/api/agent/chat')),
    JSON.stringify(turn1.failures))
  check('#1 the page rendered the semester follow-up',
    String(turn1.answer.text ?? '').includes('学期'), String(turn1.answer.text ?? '').slice(0, 80))
  check('#1 the follow-up is NOT tagged as a failure', turn1.answer.tag !== '失败', `tag=${turn1.answer.tag}`)
  check('#1 the follow-up is NOT a mock reply', turn1.answer.tag !== '占位', `tag=${turn1.answer.tag}`)
  check('#1 no /api/scores request was issued in this round (the Agent owns the score question)',
    !turn1.requests.some((r) => r.url.includes('/api/scores')),
    JSON.stringify(turn1.requests.filter((r) => r.url.includes('/api')).map((r) => r.url)))
  check('#1 no fabricated scores in the follow-up',
    !['操作系统', '数据结构', '成绩：95', '成绩：92'].some((k) => String(turn1.answer.text ?? '').includes(k)))

  // -------------------------------------------------------------------------
  // 第二轮：带同一个 threadId 补学期 → 真实成绩渲染
  // -------------------------------------------------------------------------
  const turn2 = await sendAndWait('2025-2026第一学期', '2-agent-semester')
  check('#2 the second Agent call reuses the SAME threadId',
    turn2.agentRequestBody !== null && thread1 !== null && turn2.agentRequestBody.includes(thread1),
    `${turn2.agentRequestBody} vs threadId=${thread1}`)
  check('#2 the second Agent response echoes the same threadId',
    agentThreadIdOf(turn2) === thread1, `${agentThreadIdOf(turn2)} vs ${thread1}`)
  check('#2 the page rendered the REAL rows (操作系统 95 / 数据结构 92 / 计算机网络 88)',
    ['操作系统', '数据结构', '计算机网络', '95', '92', '88'].every((k) => String(turn2.answer.text ?? '').includes(k)),
    String(turn2.answer.text ?? '').replace(/\n/g, ' ').slice(0, 160))
  check('#2 the answer is NOT tagged as a failure', turn2.answer.tag !== '失败', `tag=${turn2.answer.tag}`)
  check('#2 the answer is NOT tagged as a mock reply', turn2.answer.tag !== '占位', `tag=${turn2.answer.tag}`)
  check('#2 the semester follow-up was NOT repeated',
    !String(turn2.answer.text ?? '').includes('请问你要查询哪个学期'))

  // -------------------------------------------------------------------------
  // 第三轮：F3 原有考试 / 课程链路不受影响
  // -------------------------------------------------------------------------
  const turnExam = await sendAndWait('帮我查一下考试安排', '3-exam')
  check('#3 exam question still goes to Spring Boot /api/exams (NOT the Agent)',
    turnExam.requests.some((r) => r.url.includes('/api/exams')) &&
      !turnExam.requests.some((r) => r.url.includes('/api/agent/chat')),
    JSON.stringify(turnExam.requests.map((r) => r.url)))
  check('#3 exam answer rendered', String(turnExam.answer.text ?? '').trim().length > 0)
  check('#3 exam answer is not a failure', turnExam.answer.tag !== '失败', `tag=${turnExam.answer.tag}`)

  const turnCourse = await sendAndWait('查看第3周课程', '4-course')
  check('#4 course question still goes to Spring Boot /api/courses (NOT the Agent)',
    turnCourse.requests.some((r) => r.url.includes('/api/courses')) &&
      !turnCourse.requests.some((r) => r.url.includes('/api/agent/chat')),
    JSON.stringify(turnCourse.requests.map((r) => r.url)))
  check('#4 course answer rendered', String(turnCourse.answer.text ?? '').trim().length > 0)
  check('#4 course answer is not a failure', turnCourse.answer.tag !== '失败', `tag=${turnCourse.answer.tag}`)

  // 侧栏「清空」之后 threadId 必须复位：下一句成绩问题又是一次全新会话（不带 threadId）
  await evaluate(`(() => { document.querySelector('.chat-page__clear').click(); return true })()`)
  await sleep(600)
  const turnReset = await sendAndWait('帮我查一下2025-2026-1的成绩', '5-after-clear')
  check('#5 after 清空 the next Agent call starts a NEW conversation (no threadId in the body)',
    turnReset.agentRequestBody !== null && !/'threadId'/.test(turnReset.agentRequestBody),
    String(turnReset.agentRequestBody))
  check('#5 the new Agent threadId differs from the cleared one',
    agentThreadIdOf(turnReset) !== null && agentThreadIdOf(turnReset) !== thread1,
    `${agentThreadIdOf(turnReset)} vs ${thread1}`)
} else {
  // -------------------------------------------------------------------------
  // Agent 不可用：浏览器侧必须回退 F3 直连链路
  // -------------------------------------------------------------------------
  const turnFallback = await sendAndWait('帮我查一下2025-2026-1的成绩', 'fallback-agent-down')
  check('fallback#1 the browser did try the Agent first',
    turnFallback.requests.some((r) => r.url.includes('/api/agent/chat')),
    JSON.stringify(turnFallback.requests.map((r) => r.url)))
  check('fallback#2 the Agent call did NOT succeed (service down is visible)',
    !turnFallback.responses.some((r) => r.url.includes('/api/agent/chat') && r.status === 200),
    JSON.stringify(turnFallback.responses.filter((r) => r.url.includes('/api/agent/chat')).map((r) => r.status)))
  check('fallback#3 the browser then fell back to Spring Boot /api/scores',
    turnFallback.requests.some((r) => r.url.includes('/api/scores')),
    JSON.stringify(turnFallback.requests.map((r) => r.url)))
  check('fallback#4 the fallback query really returned data (HTTP 200)',
    turnFallback.responses.some((r) => r.url.includes('/api/scores') && r.status === 200),
    JSON.stringify(turnFallback.responses.filter((r) => r.url.includes('/api/scores')).map((r) => r.status)))
  check('fallback#5 the page rendered the REAL scores from the F3 chain',
    ['操作系统', '数据结构', '计算机网络', '成绩：95'].every((k) => String(turnFallback.answer.text ?? '').includes(k)),
    String(turnFallback.answer.text ?? '').replace(/\n/g, ' ').slice(0, 160))
  check('fallback#6 the fallback answer is NOT tagged as a failure (the fallback worked)',
    turnFallback.answer.tag !== '失败', `tag=${turnFallback.answer.tag}`)
  check('fallback#7 no fabricated data in the fallback rendering',
    !String(turnFallback.answer.text ?? '').includes('暂无数据'))
}

console.log(`CONSOLE ERRORS: ${JSON.stringify(consoleErrors)}`)

const failed = report.filter((r) => !r.ok)
const summary = `\n=== F4-1 ${LABEL} (mode=${MODE}): ${report.length - failed.length}/${report.length} passed ===\n`
console.log(summary)

writeFileSync(
  `D:/code/campus-flow/campus-web/.f4-agent-edge-${LABEL}.txt`,
  [
    `PAGE_URL=${PAGE_URL}`,
    `MODE=${MODE}`,
    `BROWSER=${version.Browser}`,
    `CONSOLE_ERRORS=${JSON.stringify(consoleErrors, null, 2)}`,
    report.map((r) => `[${r.ok ? 'PASS' : 'FAIL'}] ${r.name}${r.detail ? ` >> ${r.detail}` : ''}`).join('\n'),
    summary,
  ].join('\n'),
)

ws.close()
try { await fetch(`http://127.0.0.1:${CDP_PORT}/json/close/${page.id}`) } catch {}
edge.kill()
process.exitCode = failed.length === 0 ? 0 : 1
