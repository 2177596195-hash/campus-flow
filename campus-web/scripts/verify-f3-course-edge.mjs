/**
 * F3-2 真实浏览器端到端验证（本机 Edge + CDP）。
 *
 * 与 `verify-f3-edge.mjs` 同款姿势：用独立 user-data-dir 以 headless 模式启动
 * Edge 并打开 CDP 端口，直接驱动它完成两次真实查询：
 *   1) 输入「查看第3周课程」→ 页面内 fetch 打 /api/courses?studentId=...&week=3
 *      （经 Vite 代理到 Spring Boot）→ 读取渲染后的 DOM 文本，
 *      断言第 3 周真实课表渲染、且「数据库原理」（5-16 周开课）不出现；
 *   2) 输入「我的课程表」→ 请求**不带 week** 参数 → 断言整学期课表渲染、
 *      「数据库原理」这次出现 —— 反证第一次的 week 过滤真的生效。
 *
 * 这能定论模块级验证无法覆盖的事：**浏览器端同源代理是否真的放行**。
 *
 * 用法：node scripts/verify-f3-course-edge.mjs <pageUrl> <label>
 */

import { createRequire } from 'node:module'
import { writeFileSync, mkdirSync } from 'node:fs'
import { spawn } from 'node:child_process'

const require = createRequire('file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/')
const WebSocket = require('ws')

const PAGE_URL = process.argv[2] ?? 'http://localhost:5199/'
const LABEL = process.argv[3] ?? 'run'
const CDP_PORT = 9223
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const PROFILE = 'D:/code/campus-flow/campus-web/.edge-profile-course'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---- 启动 Edge ----
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

// ---- 等 CDP 就绪 ----
let version = null
for (let i = 0; i < 40; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)
    version = await r.json()
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
console.log(`Edge CDP ready: ${version.Browser}`)

const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
let page = targets.find((t) => t.type === 'page')
if (!page) {
  const created = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?about:blank`, { method: 'PUT' })).json()
  page = created
}

const ws = new WebSocket(page.webSocketDebuggerUrl, { perMessageDeflate: false })
await new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej) })

let nextId = 1
const pending = new Map()
const netLog = []
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
    const { url, method } = msg.params.request
    if (url.includes('/api/courses')) netLog.push({ t: 'request', url, method })
  }
  if (msg.method === 'Network.responseReceived') {
    const { url, status, headers } = msg.params.response
    if (url.includes('/api/courses')) {
      netLog.push({ t: 'response', url, status, acao: headers['access-control-allow-origin'] ?? headers['Access-Control-Allow-Origin'] ?? null })
    }
  }
  if (msg.method === 'Network.loadingFailed') {
    netLog.push({ t: 'failed', err: msg.params.errorText, blocked: msg.params.blockedReason ?? null })
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? '').join(' '))
  }
})

const send = (method, params = {}) => {
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

/** 读取当前全部 assistant 消息（文本 + 标签） */
const READ_ASSISTANTS = `
  (() => [...document.querySelectorAll('.msg--assistant')].map(n => ({
    text: n.querySelector('.msg__text')?.textContent ?? '',
    tag: n.querySelector('.msg__tag')?.textContent?.trim() ?? null,
  })))()
`

/**
 * 等待真正的回复渲染：必须出现**非空文本**的 assistant 消息。
 *
 * 不能只数 `.msg--assistant` 节点：发送瞬间 Vue 会先渲染一个
 * 「正在生成」的占位气泡（同样挂 assistant class、内容为空），
 * 数量会立刻 +1，用它当回复就全部断言在空气上。
 * 真实回复渲染后占位气泡移除、同位置出现带文本的消息。
 */
async function waitForReply(above, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const list = await evaluate(READ_ASSISTANTS)
    const last = list?.[list.length - 1]
    if (list.length > above && last && String(last.text ?? '').trim().length > 0) return list
    await sleep(300)
  }
  return null
}

await send('Network.enable')
await send('Runtime.enable')
await send('Page.enable')

// 1) 加载页面
await send('Page.navigate', { url: PAGE_URL })
await sleep(3000)

const title = await evaluate('document.title')
check('page loaded (title)', String(title).includes('CampusFlow'), String(title))

const promptInfo = await evaluate(`
  (() => [...document.querySelectorAll('.prompt')].map(b => ({
    title: b.querySelector('.prompt__title')?.textContent?.trim(),
    question: b.querySelector('.prompt__question')?.textContent?.trim(),
  })))()
`)
check('welcome panel renders example prompts', Array.isArray(promptInfo) && promptInfo.length > 0, `count=${promptInfo?.length}`)
const courseIdx = promptInfo.findIndex((p) => String(p.title ?? '').includes('课表') || String(p.title ?? '').includes('课程'))
check('course card present', courseIdx >= 0, JSON.stringify(promptInfo[courseIdx]))

// ---------- 第一次查询：带明确周次 ----------
const assistantsBefore = (await evaluate(READ_ASSISTANTS)).length

const filled = await evaluate(SET_DRAFT('查看第3周课程'))
check('typed "查看第3周课程" into the input', filled === '查看第3周课程', String(filled))

netLog.length = 0
await evaluate(`(() => { document.querySelector('.composer__send').click(); return true })()`)

let assistants1 = await waitForReply(assistantsBefore)
check('reply #1 arrived in the chat (non-empty text)',
  Array.isArray(assistants1) && assistants1.length > assistantsBefore,
  `before=${assistantsBefore} after=${assistants1?.length}`)
// 给尾随的 Network 事件一点缓冲，避免响应事件晚于 DOM 快照
await sleep(500)

const answer1 = assistants1?.[assistants1.length - 1] ?? {}
const body1 = answer1.text ?? ''
console.log(`\nNETWORK #1:\n${JSON.stringify(netLog.filter((e) => e.t !== 'failed'), null, 2)}`)
console.log(`RENDERED ASSISTANT MESSAGE #1 (tag=${answer1.tag}):\n${body1}\n`)

const reqs1 = netLog.filter((e) => e.t === 'request')
const resps1 = netLog.filter((e) => e.t === 'response')
const failures1 = netLog.filter((e) => e.t === 'failed')

check('a real request to /api/courses with studentId AND week=3 was issued by the page',
  reqs1.length >= 1 && reqs1[0].url.includes('studentId=student-001') && reqs1[0].url.includes('week=3'),
  reqs1[0]?.url ?? 'none')
check('no request-level failure (CORS/network)', failures1.length === 0, JSON.stringify(failures1))
check('browser got HTTP 200 from the real backend (via Vite proxy)',
  resps1.some((r) => r.status === 200), `status=${resps1[0]?.status}, acao=${resps1[0]?.acao}`)

check('page rendered real course 数据结构', body1.includes('数据结构'), body1.slice(0, 80))
check('page rendered real weekday 周一', body1.includes('周一'))
check('page rendered real room A302', body1.includes('A302'))
check('page rendered real teacher 李明', body1.includes('李明'))
check('page rendered the week scope wording 第 3 周', body1.includes('第 3 周'))
check('数据库原理 (starts week 5) NOT rendered for week=3', !body1.includes('数据库原理'))
check('no 占位 tag on the real answer', answer1.tag !== '占位', `tag=${answer1.tag}`)
check('no 失败 tag on the real answer', answer1.tag !== '失败', `tag=${answer1.tag}`)

// ---------- 第二次查询：不带周次（整学期） ----------
const assistantsBefore2 = (await evaluate(READ_ASSISTANTS)).length

const filled2 = await evaluate(SET_DRAFT('我的课程表'))
check('typed "我的课程表" into the input', filled2 === '我的课程表', String(filled2))

netLog.length = 0
await evaluate(`(() => { document.querySelector('.composer__send').click(); return true })()`)

let assistants2 = await waitForReply(assistantsBefore2)
check('reply #2 arrived in the chat (non-empty text)',
  Array.isArray(assistants2) && assistants2.length > assistantsBefore2,
  `before=${assistantsBefore2} after=${assistants2?.length}`)
await sleep(500)

const answer2 = assistants2?.[assistants2.length - 1] ?? {}
const body2 = answer2.text ?? ''
console.log(`NETWORK #2:\n${JSON.stringify(netLog.filter((e) => e.t !== 'failed'), null, 2)}`)
console.log(`RENDERED ASSISTANT MESSAGE #2 (tag=${answer2.tag}):\n${body2}\n`)

const reqs2 = netLog.filter((e) => e.t === 'request')
const resps2 = netLog.filter((e) => e.t === 'response')
const failures2 = netLog.filter((e) => e.t === 'failed')

check('request #2 carries studentId but NO week param',
  reqs2.length >= 1 && reqs2[0].url.includes('studentId=student-001') &&
    !new URL(reqs2[0].url).searchParams.has('week'),
  reqs2[0]?.url ?? 'none')
check('no request-level failure on #2', failures2.length === 0, JSON.stringify(failures2))
check('browser got HTTP 200 on #2', resps2.some((r) => r.status === 200), `status=${resps2[0]?.status}`)
check('page rendered the whole-semester wording 整学期', body2.includes('整学期'))
check('数据库原理 now rendered (whole semester) — proves week filter was real in #1',
  body2.includes('数据库原理'))
check('no 占位 tag on answer #2', answer2.tag !== '占位', `tag=${answer2.tag}`)
check('no 失败 tag on answer #2', answer2.tag !== '失败', `tag=${answer2.tag}`)

console.log(`CONSOLE ERRORS: ${JSON.stringify(consoleErrors)}`)

// ---------- 第三次查询：空数组（第 20 周没有任何课程覆盖）----------
// 覆盖「返回空数组」的浏览器层表现：必须是正常回复（无 失败 标记），不是错误。
const assistantsBefore3 = (await evaluate(READ_ASSISTANTS)).length

const filled3 = await evaluate(SET_DRAFT('查看第20周课程'))
check('typed "查看第20周课程" into the input', filled3 === '查看第20周课程', String(filled3))

netLog.length = 0
await evaluate(`(() => { document.querySelector('.composer__send').click(); return true })()`)

let assistants3 = await waitForReply(assistantsBefore3)
check('reply #3 arrived in the chat (non-empty text)',
  Array.isArray(assistants3) && assistants3.length > assistantsBefore3,
  `before=${assistantsBefore3} after=${assistants3?.length}`)
await sleep(500)

const answer3 = assistants3?.[assistants3.length - 1] ?? {}
const body3 = answer3.text ?? ''
console.log(`NETWORK #3:\n${JSON.stringify(netLog.filter((e) => e.t !== 'failed'), null, 2)}`)
console.log(`RENDERED ASSISTANT MESSAGE #3 (tag=${answer3.tag}):\n${body3}\n`)

const reqs3 = netLog.filter((e) => e.t === 'request')
const resps3 = netLog.filter((e) => e.t === 'response')
const failures3 = netLog.filter((e) => e.t === 'failed')

check('request #3 carries week=20', reqs3.length >= 1 && reqs3[0].url.includes('week=20'), reqs3[0]?.url ?? 'none')
check('no request-level failure on #3', failures3.length === 0, JSON.stringify(failures3))
check('browser got HTTP 200 on #3 (empty array is 200)', resps3.some((r) => r.status === 200), `status=${resps3[0]?.status}`)
check('empty week rendered as NORMAL reply (第 20 周 + 没有课程)', body3.includes('第 20 周') && body3.includes('没有课程'), body3.slice(0, 80))
check('empty week reply has NO 失败 tag', answer3.tag !== '失败', `tag=${answer3.tag}`)
check('empty week reply has NO 失败 wording', !body3.includes('课表查询失败'))
check('empty week reply contains NO fabricated courses',
  !['数据结构', '计算机网络', '数据库原理'].some((k) => body3.includes(k)))

const failed = report.filter((r) => !r.ok)
const summary = `\n=== ${LABEL}: ${report.length - failed.length}/${report.length} passed ===\n`
console.log(summary)

writeFileSync(
  `D:/code/campus-flow/campus-web/.f3-course-edge-${LABEL}.txt`,
  [
    `PAGE_URL=${PAGE_URL}`,
    `BROWSER=${version.Browser}`,
    `NETWORK=\n${JSON.stringify(netLog, null, 2)}`,
    `CONSOLE_ERRORS=${JSON.stringify(consoleErrors, null, 2)}`,
    `ANSWER_1_TAG=${answer1.tag}`,
    `ANSWER_1_TEXT:\n${body1}`,
    `ANSWER_2_TAG=${answer2.tag}`,
    `ANSWER_2_TEXT:\n${body2}`,
    `ANSWER_3_TAG=${answer3.tag}`,
    `ANSWER_3_TEXT:\n${body3}`,
    '',
    report.map((r) => `[${r.ok ? 'PASS' : 'FAIL'}] ${r.name}${r.detail ? ` >> ${r.detail}` : ''}`).join('\n'),
    summary,
  ].join('\n'),
  'utf8',
)

ws.close()
try { await fetch(`http://127.0.0.1:${CDP_PORT}/json/close/${page.id}`) } catch {}
edge.kill()
process.exitCode = failed.length === 0 ? 0 : 1
