/**
 * F4-1 静态契约核对（成绩 Agent 接入）。
 *
 * 与 `verify-f3-score-contract.mjs` 同款思路：直接断言「源码里的事实」，
 * 每条都是可复现的字符串 / 结构判定，不需要起服务。
 *
 * 覆盖：
 *   1. ChatView 的接线姿势（Agent 优先、F3 保留、失败回退、threadId 生命周期）；
 *   2. `@/api/agent` 与 `@/lib/agentScoreFlow` 的公共契约（含 F4 新增的 failureKind）；
 *   3. Vite 代理与 env example 的登记；
 *   4. Python 侧 HTTP 入口（app/api/server.py）与 main.py 的复用关系；
 *   5. 阶段边界：不接登录、不接 UI 库、不接考试 / 课程 / 请假 Agent。
 *
 * 用法：node scripts/verify-f4-agent-score-contract.mjs
 */

import { existsSync, readFileSync } from 'node:fs'

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
const chatViewCode = code('src/views/ChatView.vue')
const agentApi = read('src/api/agent.ts')
const agentFlow = read('src/lib/agentScoreFlow.ts')
const agentFlowCode = code('src/lib/agentScoreFlow.ts')
const scoreFlow = read('src/lib/scoreFlow.ts')
const scoreIntent = read('src/constants/scoreIntent.ts')
const scoresApi = read('src/api/scores.ts')
const viteConfig = read('vite.config.ts')
const envExample = read('.env.example')
const pkg = JSON.parse(read('package.json'))

const PY_ROOT = '../campus-agent'
const serverPy = `${PY_ROOT}/app/api/server.py`
const mainPy = `${PY_ROOT}/main.py`
const slotsPy = `${PY_ROOT}/app/agent/slots.py`

console.log('=== F4-1 source contract check (score agent) ===')

// ---------------------------------------------------------------------------
// 1) ChatView 接线
// ---------------------------------------------------------------------------
check('ChatView imports the Agent score orchestration',
  chatView.includes("from '@/lib/agentScoreFlow'") &&
    chatView.includes('runAgentScoreChat') &&
    chatView.includes('shouldRenderAgentReply') &&
    chatView.includes('toAgentAssistantMessage'))

// 成绩主分支 = **最后一个** `if (isScoreQuery(text))`：前面的槽位分支里也有同名判断，
// 断言必须落在主分支那段代码上，否则会拿槽位分支的代码去核对（曾经踩过）。
const scoreBranchPos = chatView.lastIndexOf('if (isScoreQuery(text))')
const examBranchPos = chatView.indexOf('if (isExamQuery(text))', scoreBranchPos)
const leaveBranchPos = chatView.indexOf('// —— 请假分流', scoreBranchPos)
const scoreBlock = chatView.slice(scoreBranchPos, leaveBranchPos)
// Agent 学期槽位那段代码（「补学期」输入的解释逻辑）
const agentSlotPos = chatView.indexOf('if (pendingAgentSemester) {')
const agentSlotBlock = chatView.slice(agentSlotPos, chatView.indexOf('// —— 成绩槽位', agentSlotPos))

check('score branch is still routed BEFORE the exam branch (F3-3 invariant kept)',
  scoreBranchPos >= 0 && examBranchPos >= 0 && scoreBranchPos < examBranchPos,
  `score@${scoreBranchPos} exam@${examBranchPos}`)
check('score branch calls the Agent path, not runScoreQuery directly',
  /replyWithAgentScore\(text\)/.test(scoreBlock) && !/runScoreQuery\(/.test(scoreBlock),
  'expect replyWithAgentScore(text) and no direct runScoreQuery inside the score branch')
check('the Agent path is preferred: "all semesters" is the only F3 shortcut',
  scoreBlock.indexOf('if (wantsAllSemesters(text))') >= 0 &&
    scoreBlock.indexOf('if (wantsAllSemesters(text))') < scoreBlock.indexOf('replyWithAgentScore(text)') &&
    scoreBlock.indexOf('dispatchF3Score(text)') < scoreBlock.indexOf('replyWithAgentScore(text)'))

// ---------------------------------------------------------------------------
// 2) 失败可区分 + 回退
// ---------------------------------------------------------------------------
check('F3 fallback entry exists and is a dedicated function', /function dispatchF3Score\(text: string\)/.test(chatView))
check('F3 fallback is invoked when the Agent request fails',
  /reply\.error\s*\)\s*\{[\s\S]*?dispatchF3Score\(text\)/.test(chatView),
  'expect dispatchF3Score(text) inside the "Agent failed" branch')
check('Agent failure kinds are consumed (not inferred from message text)',
  chatView.includes('failureKind') && !/reply\.content\.includes\('无法连接/.test(chatView))
check('failure classification comes from the flow layer',
  agentFlow.includes('failureKind?: AgentErrorKind') && agentFlow.includes('error.kind'))
check('the four failure kinds are the api layer contract',
  ['network', 'http', 'malformed', 'timeout'].every((k) => agentApi.includes(`'${k}'`)) &&
    /AgentErrorKind = 'network' \| 'http' \| 'malformed' \| 'timeout'/.test(agentApi))
check('failure is never rendered as success (no error flag stripping)',
  /if \(!reply\.error\) \{[\s\S]*?conversationStore\.appendMessage\(toAgentAssistantMessage\(reply\)\)[\s\S]*?\}/.test(chatView) &&
    chatView.includes('toAgentAssistantMessage(reply)') &&
    agentFlow.includes('...(reply.error ? { error: true } : {})'))
check('cancelled (empty) Agent payload is not rendered and does not trigger fallback',
  chatView.includes('shouldRenderAgentReply(reply)') && agentFlow.includes('content.trim().length > 0'))

// ---------------------------------------------------------------------------
// 3) threadId 生命周期
// ---------------------------------------------------------------------------
check('agentThreadId is a component-local binding (not a global)',
  /let agentThreadId: string \| null = null/.test(chatView) &&
    !/window\.agentThreadId\s*=/.test(chatViewCode) &&
    !/globalThis\.agentThreadId\s*=/.test(chatViewCode))
check('first Agent call passes no threadId', chatView.includes('runAgentScoreChat(text, agentThreadId ?? undefined, controller.signal)'))
check('threadId returned by the Agent is stored for the next turn',
  /if \(threadId\) agentThreadId = threadId/.test(chatView))
check('agentThreadId is reset on new conversation', /function handleNewConversation\(\) \{[\s\S]*?agentThreadId = null/.test(chatView))
check('agentThreadId is reset on clear conversation',
  /function handleClearConversation\(\) \{[\s\S]*?agentThreadId = null/.test(chatView))
check('agentThreadId is reset when the conversation id changes (sidebar switch)',
  /watch\(\s*\(\) => conversationStore\.currentConversationId,\s*\(current, previous\) => \{[\s\S]*?agentThreadId = null/.test(chatView))
check('the "first message creates the conversation" transition must NOT reset F4 state (real bug found by the browser run)',
  /if \(previous === null \|\| previous === undefined\) return/.test(chatView) &&
    /if \(current === previous\) return/.test(chatView))
check('no complex persistence introduced for threadId',
  !/localStorage|sessionStorage|indexedDB/i.test(chatViewCode + agentFlowCode))
check('a local slot keeps the semester follow-up on the Agent path',
  chatView.includes('pendingAgentSemester') &&
    /if \(pendingAgentSemester\) \{[\s\S]*?replyWithAgentScore\(text\)\s*\n\s*return/.test(chatView))
check('the follow-up turn is passed through to the Agent without front-end guessing',
  agentSlotBlock.includes('replyWithAgentScore(text)') &&
    !agentSlotBlock.includes('replyWithScores(') &&
    !agentSlotBlock.includes('askScoreSemester('),
  'the Agent slot block must not resolve the semester locally')

// ---------------------------------------------------------------------------
// 4) F3 代码与测试未被破坏
// ---------------------------------------------------------------------------
check('F3 score orchestration still exists and is still used',
  scoreFlow.includes('runScoreQuery') && chatView.includes("from '@/lib/scoreFlow'") &&
    /replyWithScores\(semester\)/.test(chatView))
check('F3 semester slot still exists', chatView.includes('pendingScoreSemester = true') && chatView.includes('askScoreSemester()'))
check('F3 follow-up text is still owned by scoreFlow',
  chatView.includes('SCORE_SEMESTER_FOLLOWUP') && !chatView.includes('要查哪个学期'))
check('F3 intent module untouched in shape',
  /export function isScoreQuery/.test(scoreIntent) &&
    /export function parseSemester/.test(scoreIntent) &&
    /export function wantsAllSemesters/.test(scoreIntent))
check('F3 score API module still exists', /export async function fetchScoreRecords/.test(scoresApi) && scoresApi.includes('/api/scores'))
check('F3 exam / course / leave branches unchanged',
  /if \(isExamQuery\(text\)\) \{\s*replyWithExams\(\)\s*return\s*\}/.test(chatView) &&
    /if \(isCourseQuery\(text\)\) \{\s*replyWithCourses\(parseCourseWeek\(text\)\)\s*return\s*\}/.test(chatView) &&
    /if \(isLeaveQuery\(text\)\) \{/.test(chatView))
check('mock reply still runs in the final fallback branch', /replyWithMock\(text\)/.test(chatView))
check('F3 verification scripts are all still present',
  [
    'verify-f3-contract.mjs',
    'verify-f3-exam-flow.mjs',
    'verify-f3-failure-path.mjs',
    'verify-f3-edge.mjs',
    'verify-f3-course-contract.mjs',
    'verify-f3-course-flow.mjs',
    'verify-f3-course-failure-path.mjs',
    'verify-f3-course-edge.mjs',
    'verify-f3-score-contract.mjs',
    'verify-f3-score-flow.mjs',
    'verify-f3-score-failure-path.mjs',
    'verify-f3-score-edge.mjs',
    'verify-f3-leave-contract.mjs',
    'verify-f3-leave-flow.mjs',
    'verify-f3-leave-failure-path.mjs',
  ].every((name) => existsSync(`scripts/${name}`)))

// ---------------------------------------------------------------------------
// 5) Agent 侧只接成绩（阶段边界）
// ---------------------------------------------------------------------------
check('ChatView sends ONLY score questions to the Agent',
  /if \(isScoreQuery\(text\)\) \{[\s\S]*?replyWithAgentScore\(text\)\s*\n\s*return/.test(chatView) &&
    !/isExamQuery\(text\)\) \{\s*replyWithAgentScore/.test(chatView) &&
    !/isCourseQuery\(text\)\) \{\s*replyWithAgentScore/.test(chatView) &&
    !/isLeaveQuery\(text\)\) \{\s*replyWithAgentScore/.test(chatView))

// ---------------------------------------------------------------------------
// 6) API / flow 公共契约
// ---------------------------------------------------------------------------
check('agent.ts keeps the documented chat contract',
  agentApi.includes("AGENT_CHAT_PATH = '/api/agent/chat'") &&
    agentApi.includes('method: \'POST\'') &&
    agentApi.includes('message, threadId: threadId || undefined') &&
    agentApi.includes("Accept: 'application/json'"))
check('agent.ts keeps the health contract',
  agentApi.includes("AGENT_HEALTH_PATH = '/api/agent/health'") &&
    agentApi.includes('export async function checkAgentHealth'))
check('agent.ts still uses fetch (no Axios barrel coupling)', !/from '@\/api\/index'|axios/.test(agentApi))
check('runAgentScoreChat keeps its signature (message, threadId?, signal?)',
  /export async function runAgentScoreChat\(\s*message: string,\s*threadId\?: string,\s*signal\?: AbortSignal,?\s*\)/.test(agentFlow))
check('runAgentScoreChat still never throws',
  /try \{[\s\S]*?\} catch \(error\) \{/.test(agentFlow) && !/throw new/.test(agentFlowCode))
check('AgentScoreTurn exposes reply / threadId / failureKind',
  /reply: AgentScoreReply/.test(agentFlow) && /threadId: string/.test(agentFlow) && /failureKind\?: AgentErrorKind/.test(agentFlow))
check('agentScoreFlow never fabricates score rows',
  !/数据结构|计算机网络|操作系统|高等数学|大学英语|大学物理|线性代数/.test(agentFlow))

// ---------------------------------------------------------------------------
// 7) 代理与配置登记
// ---------------------------------------------------------------------------
const agentProxyIndex = viteConfig.indexOf("'/api/agent'")
const agentTargetIndex = viteConfig.indexOf('target: AGENT_API_TARGET', agentProxyIndex)
check('vite proxies /api/agent to the Agent service',
  agentProxyIndex >= 0 &&
    agentTargetIndex > agentProxyIndex &&
    agentTargetIndex - agentProxyIndex < 300,
  `route@${agentProxyIndex} target@${agentTargetIndex}`)
check('vite keeps the Spring Boot proxy as the fallback', /'\/api': \{[\s\S]*?target: CAMPUS_API_TARGET/.test(viteConfig))
check('the more specific /api/agent rule is declared first', viteConfig.indexOf("'/api/agent'") < viteConfig.indexOf("'/api':"))
check('.env.example registers AGENT_API_TARGET', /^#?\s*AGENT_API_TARGET=/m.test(envExample))
check('.env.example registers AGENT_PORT', /^#?\s*AGENT_PORT=/m.test(envExample))
check('.env.example carries no real secret', !/sk-|api[_-]?key\s*=\s*\S/i.test(envExample))

// ---------------------------------------------------------------------------
// 8) Python HTTP 入口
// ---------------------------------------------------------------------------
check('Agent HTTP entry exists at app/api/server.py', existsSync(serverPy))
const serverCode = read(serverPy)
check('it exposes POST /api/agent/chat', serverCode.includes('@app.post("/api/agent/chat"'))
check('it exposes GET /api/agent/health', serverCode.includes('@app.get("/api/agent/health"'))
check('it reuses main._build_runtime / _invoke / _close_runtime',
  /from main import _apply_tracing, _build_runtime, _close_runtime/.test(serverCode) &&
    serverCode.includes('from main import TurnResult, _invoke') &&
    serverCode.includes('_build_runtime(settings)') &&
    serverCode.includes('_invoke('))
check('the tracing write point stays in main.py (app/** must not name it at all)',
  /_apply_tracing\(\)/.test(serverCode) &&
    !/langsmith|LangSmith/.test(serverCode) &&
    /def _apply_tracing\(\)/.test(read(mainPy)))
check('it does not re-implement the graph or the tools',
  !/build_graph\(|ToolNode|AGENT_TOOLS/.test(serverCode))
check('threadId is echoed back to the caller', serverCode.includes('threadId=thread_id'))
check('threadId is generated when missing', serverCode.includes('request.threadId or uuid.uuid4().hex'))
check('the HTTP entry marks the tracing entrypoint as http',
  serverCode.includes('ENTRYPOINT_HTTP') && read(`${PY_ROOT}/app/agent/run_context.py`).includes('ENTRYPOINT_HTTP: Final[str] = "http"'))
check('the port comes from AGENT_PORT with a default', /AGENT_PORT/.test(serverCode) && serverCode.includes('_DEFAULT_AGENT_PORT: int = 8000'))
check('the server binds to loopback only', serverCode.includes('host="127.0.0.1"'))
check('main.py exposes _build_runtime / _invoke / _close_runtime for reuse',
  /def _build_runtime\(/.test(read(mainPy)) && /def _invoke\(/.test(read(mainPy)) && /def _close_runtime\(/.test(read(mainPy)))
check('the score tool chain was NOT rewritten (CampusApiClient + MCP still the only data path)',
  read(`${PY_ROOT}/app/mcp/server.py`).includes('get_score') &&
    read(`${PY_ROOT}/app/infrastructure/campus_api/client.py`).includes('/api/scores'))
check('RAG was not wired into the score path', !/rag_service|KnowledgeRetriever/.test(serverCode))

// ---------------------------------------------------------------------------
// 9) 阶段边界
// ---------------------------------------------------------------------------
check('no JWT/login implementation added',
  !/jwt|Authorization|Bearer/i.test(agentApi + agentFlowCode + chatViewCode))
const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies })
check('no new UI component library added', !deps.some((d) => /element|antd|naive|vuetify|primevue|tailwind/i.test(d)))
check('no new front-end dependency at all was needed for F4',
  !deps.some((d) => /socket|ws|langchain|openai/i.test(d)))
check('the semester normalizer is the only Agent-side change (no graph / tool rewrite)',
  /SEMESTER_CHINESE_PATTERN/.test(read(slotsPy)) &&
    !/SEMESTER_CHINESE_PATTERN/.test(read(`${PY_ROOT}/app/agent/graph/graph.py`)))

const failed = results.filter((r) => !r.ok)
console.log(`\n=== F4-1 source contract: ${results.length - failed.length}/${results.length} passed ===`)
process.exitCode = failed.length ? 1 : 0
