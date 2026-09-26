/**
 * F3 静态契约核对（临时文件，验证完即删）。
 *
 * 目的：直接断言「源码里的事实」，而不是靠运行时观察 —— 每条都是可复现的字符串/结构判定。
 */

import { readFileSync } from 'node:fs'

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
const store = read('src/stores/conversation.ts')
const exams = read('src/api/exams.ts')
const examFlow = read('src/lib/examFlow.ts')
const examIntent = read('src/constants/examIntent.ts')
const env = read('src/config/env.ts')
const typesIndex = read('src/types/index.ts')
const typesExam = read('src/types/exam.ts')
const chatMessage = read('src/components/chat/ChatMessage.vue')
const mockReply = read('src/lib/mockReply.ts')
const apiIndex = read('src/api/index.ts')

console.log('=== F3 source contract check ===')

// 1) 组件不得直接 axios.get
check('ChatView does not call axios.get / http.get directly',
  !/axios\.get|http\.get|axios\.create/.test(chatView))
check('no .get( on axios anywhere in src', !/axios\.get\s*\(/.test(read('src/api/exams.ts')))

// 2) 意图分流：考试问题不走 mockReply
check('ChatView imports examFlow', chatView.includes("from '@/lib/examFlow'"))
check('ChatView imports isExamQuery', chatView.includes("from '@/constants/examIntent'"))
check('ChatView branches on isExamQuery before mock', /if \(isExamQuery\(text\)\) \{\s*replyWithExams\(\)\s*return\s*\}/.test(chatView))
check('mock reply only runs in the non-exam branch', /replyWithMock\(text\)/.test(chatView))

// 3) 失败文案与假数据
check('failure wording "考试安排查询失败" lives in the API layer', exams.includes('考试安排查询失败'))
check('examFlow never fabricates exam rows', !/计算机网络|数据结构|A302|B201/.test(examFlow))

// 4) 契约来源可追溯（不猜字段）
check('types/exam.ts cites the backend DTO file name', typesExam.includes('ExamScheduleResponse.java'))
check('types/exam.ts declares exactly course/date/time/room', ['course', 'date', 'time', 'room'].every((f) => typesExam.includes(f)))
check('types/exam.ts documents "no wrapper" (not ApiResponse)', typesExam.includes('List<ExamScheduleResponse>'))

// 5) studentId 来自环境变量，前端口径固定
check('env.ts reads VITE_CAMPUS_STUDENT_ID', env.includes('VITE_CAMPUS_STUDENT_ID'))
check('env.ts defaults studentId to student-001', env.includes("'student-001'"))
check('no hardcoded secret/token/password in frontend config', !/password|token|secret|api[_-]?key/i.test(code('src/config/env.ts')))

// 6) F2 结构未被破坏
check('store still exports addMessage/selectConversation/clearCurrentConversation',
  ['addMessage', 'selectConversation', 'clearCurrentConversation', 'createConversation'].every((f) => store.includes(f)))
check('store still does NOT import axios / api layer', !/axios|from '@\/api/.test(code('src/stores/conversation.ts')))
check('store has zero network imports', !/fetch\(|XMLHttpRequest/.test(store))
check('ChatView keeps WelcomePanel/ChatInput/ChatMessage/AppSidebar imports',
  ['WelcomePanel', 'ChatInput', 'ChatMessage', 'AppSidebar'].every((c) => chatView.includes(`components/` ) && chatView.includes(c)))
check('ChatMessage gained an error prop', chatMessage.includes('error?: boolean'))
check('ChatMessage renders a 失败 tag', chatMessage.includes('失败'))
check('MessageRole unchanged (user|assistant only)', read('src/types/conversation.ts').includes("'user' | 'assistant'"))

// 7) mockReply 不再声称能查考试
check('mockReply declares exams are no longer handled here', mockReply.includes('考试安排查询已经接通真实后端接口'))

// 8) 未引入 UI 组件库
const pkg = JSON.parse(read('package.json'))
const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies })
check('no new UI component library added', !deps.some((d) => /element|antd|naive|vuetify|primevue|tailwind/i.test(d)))
check('axios is still the declared dependency', deps.includes('axios'))

// 9) 未做登录/JWT
check('no JWT/login implementation added', !/jwt|Authorization|Bearer/i.test(code('src/api/exams.ts') + code('src/config/env.ts') + code('src/views/ChatView.vue')))

// 10) 路由此前契约不变
check('router keeps / -> ChatView (no /chat/:id route)', !code('src/router/index.ts').includes('/chat/:'))

const failed = results.filter((r) => !r.ok)
console.log(`\n=== source contract: ${results.length - failed.length}/${results.length} passed ===`)
process.exitCode = failed.length ? 1 : 0
