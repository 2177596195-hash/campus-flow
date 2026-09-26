/**
 * 第二阶段真实渲染验收（临时脚本，不入库）。
 *
 * 用 Vue 官方 SSR 渲染器把**真实组件**（ChatView + AppSidebar + WelcomePanel +
 * ChatMessage + ChatInput）配**真实 Pinia store** 渲染成 HTML，断言：
 *   1. 无消息时出欢迎页，且 5 个示例问题都在；
 *   2. 有消息时出消息列表，user / assistant 角色区分正确；
 *   3. 侧边栏空状态、新对话按钮、底部用户占位齐全；
 *   4. 长文本 / 换行内容能原样落到 DOM。
 *
 * 这比「源码字符串匹配」强一档：组件真的被实例化并渲染了。
 * 组件里用到的 requestAnimationFrame 等浏览器 API 在此做最小打桩。
 *
 * 通过 esbuild（vite 自带依赖）即时转译 TS / SFC，不需要额外安装。
 */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, resolve, join } from 'node:path'

const require = createRequire(import.meta.url)
const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')

/** Windows 下动态 import 绝对路径必须先转成 file:// URL */
const fileUrl = (abs) => pathToFileURL(abs).href

// ---- 最小浏览器 API 打桩（组件里只在 watch 里用到 rAF） ----
globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0)
globalThis.window = globalThis.window ?? { setTimeout, clearTimeout }

// ---- 用 vite 自带的 esbuild 转译 SFC / TS ----
const { createServer } = await import(
  fileUrl(resolve(root, 'node_modules/vite/dist/node/index.js'))
)

const server = await createServer({
  root,
  logLevel: 'error',
  server: { middlewareMode: true },
  appType: 'custom',
})

async function load(relPath) {
  const mod = await server.ssrLoadModule(relPath)
  return mod
}

const { createSSRApp } = await import(fileUrl(resolve(root, 'node_modules/vue/index.mjs')))
const { renderToString } = await import(
  fileUrl(resolve(root, 'node_modules/@vue/server-renderer/dist/server-renderer.esm-bundler.js'))
)
const { createPinia } = await import(
  fileUrl(resolve(root, 'node_modules/pinia/dist/pinia.js'))
)

const results = []
const check = (name, ok, detail = '') => results.push({ name, ok, detail })

const ChatView = (await load('/src/views/ChatView.vue')).default
const { useConversationStore } = await load('/src/stores/conversation.ts')

/** 渲染 ChatView，可选地在渲染前对 store 做初始化 */
async function renderChat(setup) {
  const app = createSSRApp(ChatView)
  const pinia = createPinia()
  app.use(pinia)
  app.config.globalProperties.$route = { name: 'chat', meta: {} }

  const store = useConversationStore(pinia)
  if (setup) setup(store)

  // 给组件里的 onMounted / rAF 一点机会执行（SSR 下 onMounted 不跑，故主要靠 setup 注入）
  return { html: await renderToString(app), store }
}

// ---------- 场景 1：无消息 -> 欢迎页 ----------
{
  const { html } = await renderChat()
  check('渲染: 欢迎页出现产品名', html.includes('CampusFlow'))
  check('渲染: 欢迎页出现定位语', html.includes('你的校园智能事务助手'))
  check('渲染: 欢迎页出现平台定位', html.includes('高校智能事务 Agent 平台'))
  for (const cap of ['查询成绩', '查询课表', '查询考试安排', '查询校园规则', '办理请假']) {
    check(`渲染: 能力项「${cap}」`, html.includes(cap))
  }
  for (const q of [
    '帮我查一下上学期的成绩',
    '我这周有哪些课？',
    '下个月的考试安排是什么？',
    '图书馆的开放时间和借阅规则是怎样的？',
    '我想请事假，需要提供哪些信息？',
  ]) {
    check(`渲染: 示例问题「${q.slice(0, 10)}…」`, html.includes(q))
  }
  check('渲染: 显示占位说明', html.includes('尚未接入后端与 Agent'))
}

// ---------- 场景 2：侧边栏 ----------
{
  const { html } = await renderChat()
  check('侧边栏: 品牌名', html.includes('CampusFlow'))
  check('侧边栏: 新对话按钮', html.includes('新对话'))
  check('侧边栏: 对话历史区标题', html.includes('对话历史'))
  check('侧边栏: 空状态文案', html.includes('还没有对话记录'))
  check('侧边栏: 底部用户占位', html.includes('未登录'))
  check('侧边栏: 提示登录待接入', html.includes('学生登录待接入'))
}

// ---------- 场景 3：有消息 -> 消息列表（user + assistant） ----------
{
  const { html, store } = await renderChat((s) => {
    s.addMessage('user', '帮我查一下上学期的成绩')
    s.addMessage('assistant', '这是第一条占位回复。\n\n第二段内容在这里。', true)
  })

  check('store: 消息数为 2', store.messages.length === 2, `got ${store.messages.length}`)
  check(
    'store: 会话标题取自首条用户消息',
    store.currentConversation?.title === '帮我查一下上学期的成绩',
    `got ${store.currentConversation?.title}`,
  )
  check('渲染: 用户消息内容出现', html.includes('帮我查一下上学期的成绩'))
  check('渲染: 助手消息内容出现', html.includes('这是第一条占位回复。'))
  check('渲染: 多段文本换行保留在 DOM', html.includes('第二段内容在这里。'))
  check('渲染: 角色标签「我」', html.includes('>我<'))
  check('渲染: 角色标签 CampusFlow（助手）', html.includes('CampusFlow'))
  check('渲染: 占位标记出现', html.includes('占位'))
  check('渲染: 欢迎页已消失', !html.includes('你的校园智能事务助手'))
  check('渲染: 输入框 placeholder', html.includes('输入校园事务'))
  check('渲染: 键盘提示文案', html.includes('Enter 发送'))
  check('渲染: Shift+Enter 提示', html.includes('Shift + Enter 换行'))
  check(
    '渲染: 空输入时发送按钮 disabled',
    /<button[^>]*disabled[^>]*>\s*发送\s*<\/button>/.test(html),
    html.match(/<button[^>]*composer__send[^>]*>/)?.[0] ?? 'no send button',
  )
}

// ---------- 场景 4：store 行为（新对话 / 清空 / 删除 / 重置） ----------
{
  const app = createSSRApp({ template: '<div/>' })
  const pinia = createPinia()
  app.use(pinia)
  const store = useConversationStore(pinia)

  // 连点新对话不堆空记录
  store.createConversation()
  store.createConversation()
  store.createConversation()
  check('store: 连点新对话复用空会话', store.conversationCount === 1, `count=${store.conversationCount}`)

  store.addMessage('user', '第一个会话的问题')
  store.createConversation()
  check('store: 有消息后再点新对话会新建', store.conversationCount === 2, `count=${store.conversationCount}`)
  check('store: 新会话消息为空', store.messages.length === 0)

  const secondId = store.currentConversationId
  store.addMessage('user', '第二个会话的问题')
  check('store: 两个会话各自持有消息', store.conversationCount === 2 && store.messages.length === 1)

  // 切换会话
  const firstId = store.conversations[1].id
  store.selectConversation(firstId)
  check('store: 切回第一个会话', store.currentConversationId === firstId)
  check('store: 第一个会话消息仍在', store.messages[0].content === '第一个会话的问题')

  // 清空当前会话
  store.clearCurrentConversation()
  check('store: 清空后消息为 0', store.messages.length === 0)
  check('store: 清空后标题复位', store.currentConversation?.title === '新对话')
  check('store: 清空不删除会话本身', store.conversationCount === 2)

  // 删除会话
  store.selectConversation(secondId)
  store.removeConversation(secondId)
  check('store: 删除后会话数 -1', store.conversationCount === 1, `count=${store.conversationCount}`)
  check('store: 删除当前会话后回退到可用会话', store.currentConversationId === firstId)

  // 重置
  store.reset()
  check('store: 重置后会话清空', store.conversationCount === 0)
  check('store: 重置后 currentConversationId 为 null', store.currentConversationId === null)
  check('store: 重置后 messages 为空数组', Array.isArray(store.messages) && store.messages.length === 0)
}

// ---------- 场景 5：长文本 ----------
{
  const long = 'A'.repeat(400) + '\n' + '中文长句'.repeat(60)
  const { html } = await renderChat((s) => {
    s.addMessage('user', long)
  })
  check('渲染: 超长文本未截断', html.includes('A'.repeat(400)))
  check('渲染: 无横向滚动内联样式', !html.includes('overflow-x: scroll'))
}

await server.close()

const failed = results.filter((r) => !r.ok)
for (const r of results) {
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : ' :: ' + r.detail}`)
}
console.log(`\n合计 ${results.length} 项，通过 ${results.length - failed.length}，失败 ${failed.length}`)
process.exit(failed.length === 0 ? 0 : 1)
