/**
 * F3-3 真实浏览器端到端验证（本机 Edge + CDP）。
 *
 * 与 `verify-f3-edge.mjs` / `verify-f3-course-edge.mjs` 同款姿势：用独立
 * user-data-dir 以 headless 模式启动 Edge 并打开 CDP 端口，驱动它完成
 * 四幕真实交互（Vue → Vite proxy → Spring Boot → MySQL → 页面渲染）：
 *   1) 「查一下我的成绩」（没给学期）→ **不发任何请求**，只回一句学期追问；
 *   2) 补上「2025-2026-1」→ 页面内 fetch 打 /api/scores?studentId=..&semester=..
 *      → 断言真实三条成绩渲染、score/credit 数值语义正确（成绩：92 / 学分：3.5）；
 *   3) 「查询2026-2027第一学期成绩」（中文学期写法，学期不存在）→
 *      断言请求里 semester=2026-2027-1、返回 200+[]、页面把空数组渲染成
 *      「该学期暂无成绩记录」的正常回复（无失败标记、无编造数据）；
 *   4) 「查一下我的全部成绩」（明确选择全部）→ 请求**不带 semester** 参数 →
 *      断言两个学期的五条成绩全部渲染（2024-2025-2 的高等数学也出现）。
 *
 * 这能定论模块级验证无法覆盖的事：浏览器端同源代理是否真的放行、
 * 学期槽位（追问 → 下一轮补齐）在真实 DOM 上的行为是否正确。
 *
 * 用法：node scripts/verify-f3-score-edge.mjs <pageUrl> <label>
 */

import { createRequire } from 'node:module'
import { writeFileSync, mkdirSync } from 'node:fs'
import { spawn } from 'node:child_process'

const require = createRequire('file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/')
const WebSocket = require('ws')

const PAGE_URL = process.argv[2] ?? 'http://localhost:5199/'
const LABEL = process.argv[3] ?? 'run'
const CDP_PORT = 9224
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const PROFILE = 'D:/code/campus-flow/campus-web/.edge-profile-score'

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
    if (url.includes('/api/scores')) netLog.push({ t: 'request', url, method })
  }
  if (msg.method === 'Network.responseReceived') {
    const { url, status, headers } = msg.params.response
    if (url.includes('/api/scores')) {
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

/** 发送一条输入并等回复，返回 {list, netLogSnapshot, answer} */
async function sendAndWait(text, label) {
  const before = (await evaluate(READ_ASSISTANTS)).length
  const filled = await evaluate(SET_DRAFT(text))
  check(`typed "${text}"`, filled === text, String(filled))
  netLog.length = 0
  await evaluate(`(() => { document.querySelector('.composer__send').click(); return true })()`)
  const list = await waitForReply(before)
  check(`reply arrived for #${label} (non-empty text)`,
    Array.isArray(list) && list.length > before, `before=${before} after=${list?.length}`)
  await sleep(500) // 给尾随的 Network 事件一点缓冲
  const answer = list?.[list.length - 1] ?? {}
  console.log(`\nNETWORK #${label}:\n${JSON.stringify(netLog.filter((e) => e.t !== 'failed'), null, 2)}`)
  console.log(`RENDERED ASSISTANT MESSAGE #${label} (tag=${answer.tag}):\n${answer.text ?? ''}\n`)
  return { list, answer }
}

await send('Network.enable')
await send('Runtime.enable')
await send('Page.enable')

// 1) 加载页面
await send('Page.navigate', { url: PAGE_URL })
await sleep(3000)

const title = await evaluate('document.title')
check('page loaded (title)', String(title).includes('CampusFlow'), String(title))

// ---------- 第一幕：没给学期 → 只追问、不发请求 ----------
{
  const { answer, list } = await sendAndWait('查一下我的成绩', '1-followup')
  const body = answer.text ?? ''
  const reqs = netLog.filter((e) => e.t === 'request')
  const failures = netLog.filter((e) => e.t === 'failed')

  check('#1 NO request was issued (semester missing -> ask, do not call the API)',
    reqs.length === 0, JSON.stringify(reqs))
  check('#1 no request-level failure', failures.length === 0, JSON.stringify(failures))
  check('#1 follow-up asks for the semester', body.includes('学期'), body.slice(0, 80))
  check('#1 follow-up shows the backend format example 2025-2026-1', body.includes('2025-2026-1'))
  check('#1 follow-up mentions the 全部 option', body.includes('全部'))
  check('#1 follow-up has NO 失败 tag (it is not an error)', answer.tag !== '失败', `tag=${answer.tag}`)
  check('#1 follow-up has NO 占位 tag (it is not a mock reply)', answer.tag !== '占位', `tag=${answer.tag}`)
  check('#1 follow-up does not fabricate any scores',
    !['数据结构', '92', '88', '95'].some((k) => body.includes(k)), body.slice(0, 120))
  void list
}

// ---------- 第二幕：补上学期 → 真实查询渲染 ----------
{
  const { answer } = await sendAndWait('2025-2026-1', '2-semester')
  const body = answer.text ?? ''
  const reqs = netLog.filter((e) => e.t === 'request')
  const resps = netLog.filter((e) => e.t === 'response')
  const failures = netLog.filter((e) => e.t === 'failed')

  check('#2 request carries studentId AND semester=2025-2026-1',
    reqs.length >= 1 && reqs[0].url.includes('studentId=student-001') && reqs[0].url.includes('semester=2025-2026-1'),
    reqs[0]?.url ?? 'none')
  check('#2 no request-level failure (same-origin proxy works)', failures.length === 0, JSON.stringify(failures))
  check('#2 browser got HTTP 200 from the real backend (via Vite proxy)',
    resps.some((r) => r.status === 200), `status=${resps[0]?.status}, acao=${resps[0]?.acao}`)
  check('#2 page rendered the scope wording 2025-2026-1 学期成绩', body.includes('2025-2026-1 学期成绩'))
  check('#2 page rendered the count 共 3 门', body.includes('共 3 门'))
  check('#2 page rendered real courses (操作系统/数据结构/计算机网络)',
    ['操作系统', '数据结构', '计算机网络'].every((k) => body.includes(k)))
  check('#2 page rendered NUMERIC semantics: 成绩：95 / 成绩：92 / 学分：3.5',
    body.includes('成绩：95') && body.includes('成绩：92') && body.includes('学分：3.5'))
  check('#2 no 失败 tag on the real answer', answer.tag !== '失败', `tag=${answer.tag}`)
  check('#2 no 占位 tag on the real answer', answer.tag !== '占位', `tag=${answer.tag}`)
}

// ---------- 第三幕：中文学期写法 + 不存在的学期 → 空结果正常渲染 ----------
{
  const { answer } = await sendAndWait('查询2026-2027第一学期成绩', '3-chinese-empty')
  const body = answer.text ?? ''
  const reqs = netLog.filter((e) => e.t === 'request')
  const resps = netLog.filter((e) => e.t === 'response')
  const failures = netLog.filter((e) => e.t === 'failed')

  check('#3 Chinese semester form parsed into backend format (semester=2026-2027-1)',
    reqs.length >= 1 && reqs[0].url.includes('semester=2026-2027-1'), reqs[0]?.url ?? 'none')
  check('#3 no request-level failure', failures.length === 0, JSON.stringify(failures))
  check('#3 browser got HTTP 200 (empty array is 200, not 404)',
    resps.some((r) => r.status === 200), `status=${resps[0]?.status}`)
  check('#3 empty semester rendered as NORMAL reply (该学期暂无成绩记录)',
    body.includes('2026-2027-1') && body.includes('该学期暂无成绩记录'), body.slice(0, 100))
  check('#3 empty reply has NO 失败 tag', answer.tag !== '失败', `tag=${answer.tag}`)
  check('#3 empty reply contains NO fabricated scores',
    !['数据结构', '计算机网络', '操作系统', '92', '88', '95'].some((k) => body.includes(k)))
}

// ---------- 第四幕：明确「全部」 → 请求不带 semester，渲染全部学期 ----------
{
  const { answer } = await sendAndWait('查一下我的全部成绩', '4-all-semesters')
  const body = answer.text ?? ''
  const reqs = netLog.filter((e) => e.t === 'request')
  const resps = netLog.filter((e) => e.t === 'response')
  const failures = netLog.filter((e) => e.t === 'failed')

  check('#4 explicit 全部 -> request carries studentId but NO semester param',
    reqs.length >= 1 && reqs[0].url.includes('studentId=student-001') &&
      !new URL(reqs[0].url).searchParams.has('semester'),
    reqs[0]?.url ?? 'none')
  check('#4 no request-level failure', failures.length === 0, JSON.stringify(failures))
  check('#4 browser got HTTP 200', resps.some((r) => r.status === 200), `status=${resps[0]?.status}`)
  check('#4 page rendered the whole-history wording 全部学期成绩', body.includes('全部学期成绩'))
  check('#4 page rendered the count 共 5 门', body.includes('共 5 门'))
  check('#4 older semester rows rendered too (2024-2025-2 高等数学 成绩：84)',
    body.includes('2024-2025-2') && body.includes('高等数学') && body.includes('成绩：84'))
  check('#4 no 失败 tag', answer.tag !== '失败', `tag=${answer.tag}`)
}

console.log(`CONSOLE ERRORS: ${JSON.stringify(consoleErrors)}`)

const failed = report.filter((r) => !r.ok)
const summary = `\n=== ${LABEL}: ${report.length - failed.length}/${report.length} passed ===\n`
console.log(summary)

writeFileSync(
  `D:/code/campus-flow/campus-web/.f3-score-edge-${LABEL}.txt`,
  [
    `PAGE_URL=${PAGE_URL}`,
    `BROWSER=${version.Browser}`,
    `NETWORK=\n${JSON.stringify(netLog, null, 2)}`,
    `CONSOLE_ERRORS=${JSON.stringify(consoleErrors, null, 2)}`,
    report.map((r) => `[${r.ok ? 'PASS' : 'FAIL'}] ${r.name}${r.detail ? ` >> ${r.detail}` : ''}`).join('\n'),
    summary,
  ].join('\n'),
  'utf8',
)

ws.close()
try { await fetch(`http://127.0.0.1:${CDP_PORT}/json/close/${page.id}`) } catch {}
edge.kill()
process.exitCode = failed.length === 0 ? 0 : 1
