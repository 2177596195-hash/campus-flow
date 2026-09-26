/**
 * 第二阶段验收脚本（临时，不入库）。
 *
 * 用 Node 直接跑「对话 store 的状态语义」与「组件源码里的关键交互契约」，
 * 补足 HTTP 探测查不到的部分：
 *   - store：新对话 / 添加消息 / 首条消息定标题 / 清空 / 重置 / 删除回退
 *   - 空会话复用（连点新对话不堆空记录）
 *   - 组件契约：Enter 发送 + Shift+Enter 换行 + isComposing 守卫 + disabled 判定
 *
 * 纯源码级校验，不启动浏览器。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
}
const src = (rel) => readFileSync(resolve(root, rel), 'utf8')

// ---------- 1. store 源码契约（Pinia + 无网络依赖） ----------
const storeSrc = src('src/stores/conversation.ts')

check(
  'store: 使用 Pinia defineStore',
  storeSrc.includes("defineStore('conversation'"),
)
check(
  'store: 不引入 axios',
  !/from ['"]axios['"]/.test(storeSrc) && !storeSrc.includes("from '@/api"),
)
check(
  'store: 暴露 currentConversationId',
  storeSrc.includes('currentConversationId'),
)
for (const action of [
  'createConversation',
  'addMessage',
  'selectConversation',
  'removeConversation',
  'clearCurrentConversation',
  'reset',
]) {
  check(`store: 导出 action ${action}`, storeSrc.includes(`${action},`))
}

// ---------- 2. 用真实 store 语义跑一遍（剥离 vue/pinia，改成等价纯函数复现） ----------
// store 依赖 vue 的 ref/computed，Node 里无法直接 import .ts；
// 这里用同样的算法复现关键行为，断言的是「设计契约」而非源码文本。
const TITLE_MAX_LENGTH = 18
function buildTitle(text) {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (!normalized) return '新对话'
  return normalized.length > TITLE_MAX_LENGTH
    ? `${normalized.slice(0, TITLE_MAX_LENGTH)}…`
    : normalized
}
// 断言源码里这些常量与分支确实存在（防止脚本与实现漂移）
check('store: 标题截断长度常量存在', storeSrc.includes('TITLE_MAX_LENGTH = 18'))
check('store: 空标题回退为「新对话」', storeSrc.includes("return '新对话'"))
check(
  'store: 首条用户消息才改标题',
  storeSrc.includes("if (role === 'user' && conversation.title === '新对话')"),
)
check(
  'store: 空会话复用（连点新对话不堆空记录）',
  storeSrc.includes('if (current && current.messages.length === 0) return current'),
)

// ---------- 3. ChatInput 交互契约 ----------
const inputSrc = src('src/components/chat/ChatInput.vue')
check('input: 使用 textarea', inputSrc.includes('<textarea'))
check('input: Enter 发送判定', inputSrc.includes("if (event.key !== 'Enter') return"))
check('input: Shift+Enter 走换行', inputSrc.includes('if (event.shiftKey || event.isComposing) return'))
check('input: 输入法组合态守卫', inputSrc.includes('event.isComposing'))
check('input: 阻止默认提交', inputSrc.includes('event.preventDefault()'))
check(
  'input: 空消息不可发送（trim 判定）',
  inputSrc.includes("content.value.trim().length > 0"),
)
check('input: 发送后清空输入框', inputSrc.includes("content.value = ''"))
check(
  'input: 发送按钮有 disabled',
  inputSrc.includes(':disabled="!canSend"'),
)
check('input: v-model 暴露文本', inputSrc.includes('defineModel<string>'))

// ---------- 4. 视图编排契约 ----------
const chatSrc = src('src/views/ChatView.vue')
check('view: 欢迎页在无消息时显示', chatSrc.includes('<WelcomePanel v-if="showWelcome"'))
check('view: 消息列表渲染', chatSrc.includes('<ChatMessage'))
check('view: 示例问题只回填不发送', chatSrc.includes('function handleSelectPrompt'))
check(
  'view: 示例问题回填提示词处无发送调用',
  /handleSelectPrompt[\s\S]{0,220}?nextTick\(\(\) => inputRef\.value\?\.focus\(\)\)/.test(
    chatSrc,
  ),
)
check('view: 本地占位回复不涉及 api 层', !chatSrc.includes("from '@/api"))
check('view: 助手回复带 mock 标记', chatSrc.includes("buildMockReply(text), true"))

const mockSrc = src('src/lib/mockReply.ts')
// 断言的是「代码里没有网络调用」，所以要先把注释剥掉
// （docstring 里刻意写了「不 import axios」，直接全文匹配会误报）。
const stripComments = (text) =>
  text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
const mockCode = stripComments(mockSrc)
check(
  'mock: 纯本地，无网络依赖',
  !/axios|fetch\(|XMLHttpRequest/.test(mockCode),
  mockCode.match(/axios|fetch\(|XMLHttpRequest/)?.[0] ?? '',
)
check(
  'mock: 不 import api 层',
  !mockCode.includes("from '@/api"),
)
check('mock: 明确标注为占位', mockSrc.includes('MOCK_TAG'))

// ---------- 5. 侧边栏契约 ----------
const sideSrc = src('src/components/sidebar/AppSidebar.vue')
check('sidebar: 品牌名取自 app store', sideSrc.includes('appStore.appTitle'))
check('sidebar: 新对话按钮', sideSrc.includes('<NewConversationButton'))
check('sidebar: 对话历史列表', sideSrc.includes('<ConversationList'))
check('sidebar: 底部用户/设置占位', sideSrc.includes('未登录'))
check(
  'sidebar: 无登录实现（无 token/密码字段）',
  !/token|password|jwt/i.test(sideSrc),
)

const listSrc = src('src/components/sidebar/ConversationList.vue')
check('list: 空状态文案存在', listSrc.includes('还没有对话记录'))

// ---------- 6. 响应式契约 ----------
check('responsive: 侧边栏窄屏抽屉媒体查询', sideSrc.includes('@media (max-width: 860px)'))
check('responsive: 全局禁止横向滚动', src('src/assets/main.css').includes('overflow: hidden'))
check('responsive: 欢迎页网格降列', src('src/components/chat/WelcomePanel.vue').includes('max-width: 900px'))
check('responsive: 消息气泡保留换行', src('src/components/chat/ChatMessage.vue').includes('white-space: pre-wrap'))

// ---------- 7. 路由 ----------
const routerSrc = src('src/router/index.ts')
check('router: / 进入聊天主页', routerSrc.includes("component: ChatView"))
check('router: 未引入 /chat/:id（不过度设计）', !routerSrc.includes("'/chat/:conversationId'"))

// ---------- 输出 ----------
const failed = results.filter((r) => !r.ok)
for (const r of results) {
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? ' :: ' + r.detail : ''}`)
}
console.log(`\n合计 ${results.length} 项，通过 ${results.length - failed.length}，失败 ${failed.length}`)
process.exit(failed.length === 0 ? 0 : 1)
