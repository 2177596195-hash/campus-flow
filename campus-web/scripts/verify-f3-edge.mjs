/**
 * F3 真实浏览器端到端验证（本机 Edge + CDP）。
 *
 * 本机没有 Chrome，但有 Microsoft Edge（同属 Chromium）→ 用独立 user-data-dir
 * 以 headless 模式启动 Edge 并打开 CDP 端口，直接驱动它完成：
 *   真实页面加载 → 点击「查询考试安排」示例卡片 → 点击发送
 *   → 页面内 fetch 打真实 Spring Boot → 读取渲染后的 DOM 文本
 *
 * 这能定论一件模块级验证无法覆盖的事：**浏览器端 CORS 是否真的放行**。
 *
 * 用法：node scripts/verify-f3-edge.mjs <pageUrl> <label>
 */

import { createRequire } from 'node:module'
import { writeFileSync, mkdirSync } from 'node:fs'
import { spawn } from 'node:child_process'

const require = createRequire('file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/')
const WebSocket = require('ws')

const PAGE_URL = process.argv[2] ?? 'http://localhost:5199/'
const LABEL = process.argv[3] ?? 'run'
const CDP_PORT = 9222
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const PROFILE = 'D:/code/campus-flow/campus-web/.edge-profile'

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
    if (url.includes('/api/exams')) netLog.push({ t: 'request', url, method })
  }
  if (msg.method === 'Network.responseReceived') {
    const { url, status, headers } = msg.params.response
    if (url.includes('/api/exams')) {
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
const examIdx = promptInfo.findIndex((p) => String(p.title ?? '').includes('考试'))
check('exam card present', examIdx >= 0, JSON.stringify(promptInfo[examIdx]))

// 2) 点卡片 → 回填
await evaluate(`document.querySelectorAll('.prompt')[${examIdx}].click()`)
await sleep(400)
const draft = await evaluate(`document.querySelector('textarea')?.value ?? ''`)
check('card click fills the input', String(draft).includes('考试'), String(draft))

// 3) 发送 → 真实请求
netLog.length = 0
await evaluate(`(() => { document.querySelector('.composer__send').click(); return true })()`)
await sleep(3500)

const assist = await evaluate(`
  (() => [...document.querySelectorAll('.msg--assistant')].map(n => ({
    text: n.querySelector('.msg__text')?.textContent ?? '',
    tag: n.querySelector('.msg__tag')?.textContent?.trim() ?? null,
  })))()
`)
const answer = assist[assist.length - 1]
const body = answer?.text ?? ''

console.log(`\nNETWORK:\n${JSON.stringify(netLog, null, 2)}`)
console.log(`CONSOLE ERRORS: ${JSON.stringify(consoleErrors)}`)
console.log(`\nRENDERED ASSISTANT MESSAGE (tag=${answer?.tag}):\n${body}\n`)

const reqs = netLog.filter((e) => e.t === 'request')
const resps = netLog.filter((e) => e.t === 'response')
const failures = netLog.filter((e) => e.t === 'failed')

// 4) 断言
check('a real XHR/fetch to /api/exams was issued by the page',
  reqs.length >= 1 && reqs[0].url.includes('studentId=student-001'), reqs[0]?.url ?? 'none')
check('no request-level failure (CORS/network)', failures.length === 0, JSON.stringify(failures))

if (resps.some((r) => r.status === 200)) {
  check('browser got HTTP 200 from the real backend', true, `status=${resps[0].status}, acao=${resps[0].acao}`)
  check('page rendered real course 计算机网络', body.includes('计算机网络'), body.slice(0, 80))
  check('page rendered real room A302', body.includes('A302'))
  check('page rendered real date 2026-09-28', body.includes('2026-09-28'))
  check('page rendered real time 14:00', body.includes('14:00'))
  check('page rendered second real course 数据结构', body.includes('数据结构'))
  check('page rendered second real room B201', body.includes('B201'))
  check('no 占位 tag on the real answer', answer?.tag !== '占位', `tag=${answer?.tag}`)
  check('no 失败 tag on the real answer', answer?.tag !== '失败', `tag=${answer?.tag}`)
} else {
  check('failure text shown instead', body.includes('考试安排查询失败'), body.slice(0, 120))
  check('failure answer tagged 失败', answer?.tag === '失败', `tag=${answer?.tag}`)
  check('NO fabricated exam data on failure',
    !['计算机网络', '数据结构', 'A302', 'B201', '2026-09-28'].some((k) => body.includes(k)))
}

const failed = report.filter((r) => !r.ok)
const summary = `\n=== ${LABEL}: ${report.length - failed.length}/${report.length} passed ===\n`
console.log(summary)

writeFileSync(
  `D:/code/campus-flow/campus-web/.f3-edge-${LABEL}.txt`,
  [
    `PAGE_URL=${PAGE_URL}`,
    `BROWSER=${version.Browser}`,
    `NETWORK=${JSON.stringify(netLog, null, 2)}`,
    `CONSOLE_ERRORS=${JSON.stringify(consoleErrors, null, 2)}`,
    `LAST_ASSISTANT_TAG=${answer?.tag}`,
    `LAST_ASSISTANT_TEXT:\n${body}`,
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
