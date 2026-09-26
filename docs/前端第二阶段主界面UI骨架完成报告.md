# 前端第二阶段（F2）：主界面 UI 骨架 完成报告

把占位首页改造成 CampusFlow 的 AI 校园助手主界面：**侧边栏 + 欢迎页 + 聊天区 + 输入区**。
本阶段只做前端 UI 与本地状态，**未接任何后端**（不接 Python Agent、不接 Spring Boot、不接 MCP / RAG）。

---

## 1. 环境与验证结论

| 项目 | 结果 |
|---|---|
| `npm run build` | **EXIT=0** — `vue-tsc --build` 零 TS 报错 → `55 modules transformed` |
| 产物 | `index.html 0.42kB` / `css 11.30kB (gzip 2.57)` / `js 108.34kB (gzip 42.96)`，`built in 1.02s` |
| `npm run type-check` | **EXIT=0** |
| `npm run dev` | 启动成功，`http://localhost:5173/` 返回 **200** |
| 后端改动 | **零**（见 §6） |

**验证方式说明**：本机 Chromium 下载连续两次超时（Google CDN 限速，196MB 只到 ~10%），
无法驱动真实浏览器。因此改用**两层更强的替代验证**（都是真实执行，不是静态阅读）：

1. **真实组件渲染验证**（`scripts/verify-stage2-render.mjs`，**49 项全绿**）——
   用 Vue 官方 SSR 渲染器把 **真实 SFC 组件** + **真实 Pinia store** 实例化并渲染成 HTML，
   断言欢迎页 / 示例问题 / 侧边栏 / 消息列表 / 角色标签 / disabled 态 / 长文本真的落到 DOM。
2. **契约扫描验证**（`scripts/verify-stage2.mjs`，**43 项全绿**）——断言键盘语义、
   store 不引入 axios、mock 模块无网络调用、响应式媒体查询等契约。

外加 http 层探测：dev server 与 preview server 对首页及全部关键模块均返回 **200**
（`/src/views/ChatView.vue`、`/src/stores/conversation.ts`、各组件均通过 Vite 转译）；
生产产物 CSS 中确认 `.sidebar--open` / `translate(-100%)` / 三处媒体查询真实产出。

---

## 2. 本阶段改动的文件

### 新增（14）

| 文件 | 职责 |
|---|---|
| `src/views/ChatView.vue` | 页面编排：侧边栏 + 消息区 + 输入区；发送流程、窄屏抽屉、滚动跟随 |
| `src/components/sidebar/AppSidebar.vue` | 侧边栏容器：品牌区 + 新对话 + 历史列表 + 底部用户占位 |
| `src/components/sidebar/NewConversationButton.vue` | 「新对话」按钮（只抛事件） |
| `src/components/sidebar/ConversationList.vue` | 对话历史列表 + **空状态** + 选中/删除 |
| `src/components/chat/WelcomePanel.vue` | 欢迎页：产品定位 + 5 项能力 + 示例问题网格 |
| `src/components/chat/ExamplePrompt.vue` | 单张示例问题卡片（点击只抛事件） |
| `src/components/chat/ChatMessage.vue` | 单条消息：角色区分 + 长文本换行 + 占位状态 |
| `src/components/chat/ChatInput.vue` | textarea + 发送按钮；**Enter 发送 / Shift+Enter 换行** |
| `src/stores/conversation.ts` | **Pinia conversation store**（纯本地状态，零网络） |
| `src/types/conversation.ts` | `ChatMessage` / `Conversation` / `MessageRole` |
| `src/lib/mockReply.ts` | 本地占位助手回复（关键词规则表，纯函数） |
| `src/constants/examples.ts` | 示例问题 + 能力清单（单一维护点） |
| `scripts/verify-stage2.mjs` | 契约扫描验收（43 项） |
| `scripts/verify-stage2-render.mjs` | 真实组件渲染验收（49 项） |

### 修改（6）

| 文件 | 改动 |
|---|---|
| `src/assets/main.css` | 扩充设计变量（中性色阶 / 文本层级 / 强调色 / 布局尺寸）；`html,body,#app { height:100% }`；`overflow:hidden` 兜底防横向滚动 |
| `src/layouts/DefaultLayout.vue` | 去掉原页头，改为占满视口的外壳（聊天页自带侧边栏） |
| `src/router/index.ts` | `/` → `ChatView`（name 由 `home` 改为 `chat`）；兜底重定向同步；新增 `afterEach` 写 `document.title` |
| `src/stores/app.ts` | 新增 `appSubtitle`（侧边栏品牌区使用），其余不动 |
| `src/types/index.ts` | 追加 re-export 对话类型 |
| `src/views/ChatView.vue` 等 | 见上表 |

### 删除（2）

| 文件 | 原因 |
|---|---|
| `src/views/HomeView.vue` | 第一阶段四件套自检占位页，已被 `ChatView` 取代 |
| `src/components/EnvironmentChecker.vue` | 仅被上者使用；两者删除后**零残留引用**（已 Grep 确认） |

---

## 3. 实现要点

### 3.1 Sidebar
品牌区（强调色方块 `CF` + `CampusFlow` + 「智校园 Agent」）；「新对话」按钮；
「对话历史」列表（标题单行省略 + 更新时间 + 条数，hover 才显示删除按钮）；
空状态文案「还没有对话记录 / 点击上方「新对话」开始」；
底部用户区占位（`未登录` + 「学生登录待接入」）——**未实现任何登录逻辑**。

### 3.2 欢迎页（无消息时）
`智校园 · 高校智能事务 Agent 平台` → `CampusFlow` → `你的校园智能事务助手` →
一句价值描述 → 5 项能力标签（查询成绩 / 查询课表 / 查询考试安排 / 查询校园规则 / 办理请假）
→ 5 张示例问题卡片（三列网格）→ 一行「尚未接入后端与 Agent」的说明。

**示例问题点击只回填输入框并聚焦**，不发送、不发请求（有专项断言）。

### 3.3 输入区键盘语义
| 按键 | 行为 |
|---|---|
| `Enter` | 发送 |
| `Shift + Enter` | 换行（不拦截，走 textarea 原生行为） |
| 输入法组合态 `Enter` | 不发送（`event.isComposing` 守卫，中文候选框确认不会误发） |
| 空消息（含纯空白） | 不发送；发送按钮 `disabled` |

发送后清空输入框并把高度收回一行；`textarea` 自适应高度，超过 11rem 内部滚动。
输入框是主区域视觉焦点：聚焦时描边 + 柔和外圈（`box-shadow` 单层，非渐变）。

### 3.4 mock 助手回复
`src/lib/mockReply.ts` 关键词规则表（成绩 / 课表 / 考试 / 请假 / 规则）→ 命中返回对应说明，
未命中给兜底文案。回复**恒带前缀**「（本地占位回复，尚未接入 Agent）」并在 UI 上打「占位」标签。
**所有回复都明确说「尚未接入」，不编造任何业务数据**，避免假数据被误当成真实查询结果。
420ms 的本地计时模拟「思考」，等待期间输入区禁用、消息区显示三点占位。

### 3.5 Pinia store
```
conversations: Conversation[]      currentConversationId: string | null
currentConversation / messages / hasMessages / conversationCount（computed）
createConversation / addMessage / selectConversation / removeConversation
clearCurrentConversation / reset
```
两处刻意的交互细节：
- **连点「新对话」复用空会话**，不堆一串空记录；
- **首条用户消息才决定会话标题**（截断 18 字），之后不再改动，列表认知稳定。

**数据结构保持扁平，未提前设计后端数据库模型。** store 内**不引入 axios、不 import api 层**（有断言）。

### 3.6 CSS / 响应式
原生 CSS，无 UI 组件库、无 Tailwind；**无动画炫技、无渐变**（唯一 transition 是抽屉位移）。
变量集中在 `main.css`：中性色阶 + 三级文本色 + 一个克制蓝强调色 + 两处布局尺寸。
层次靠「侧边栏浅灰底 `#f9fafb` vs 主区纯白 + 1px 分隔线」建立。

| 断点 | 行为 |
|---|---|
| `> 860px` | 侧边栏常驻左侧 |
| `≤ 860px` | 侧边栏变**覆盖式抽屉**（`translate(-100%)` 收起，顶栏出现 ☰ 按钮，点遮罩/会话后关闭） |
| `≤ 900px` | 示例问题网格 3 列 → 2 列 |
| `≤ 560px` | 网格 1 列，页边距收紧，标题降字号 |

防横向滚动三层：`html,body { overflow:hidden }`、主区 `overflow-x:hidden`、
文本 `overflow-wrap:anywhere` + `white-space:pre-wrap`。已验证超长文本（400 字符无空格 + 中文长句）不截断。

### 3.7 Router
保持第一阶段结构（布局路由 + 子路由 + 兜底重定向）。`/` 进聊天主页。
**未引入 `/chat/:conversationId`** —— 会话已在内存中承载，加路径段不带来额外能力，
等真正需要「刷新后定位到某个会话」时再加（有断言防止过度设计）。

---

## 4. 验证证据（逐项）

### 4.1 构建
```
> vue-tsc --build && vite build
✓ 55 modules transformed.
dist/index.html                   0.42 kB │ gzip:  0.30 kB
dist/assets/index-C1qEbbwG.css   11.30 kB │ gzip:  2.57 kB
dist/assets/index-DuWz988.js    108.34 kB │ gzip: 42.96 kB
✓ built in 1.02s          BUILD_EXIT=0
```

### 4.2 真实渲染验证（49 / 49 通过）
覆盖：欢迎页产品名/定位语/平台定位/5 项能力/5 个示例问题；
侧边栏品牌/新对话/历史标题/空状态/底部占位；
store 消息数 + 首条消息定标题；用户与助手消息内容落 DOM；多段文本换行保留；
角色标签「我」与 `CampusFlow`；欢迎页有消息时消失；输入框 placeholder 与键盘提示；
**空输入时发送按钮 disabled**；
store 行为：连点新对话复用空会话 / 有消息后再点会新建 / 两会话各自持有消息 /
切换回旧会话消息仍在 / 清空后消息为 0 且标题复位且不删会话 / 删除后回退到可用会话 /
重置后 `conversationCount=0` 且 `currentConversationId=null`；超长文本不截断。

### 4.3 契约扫描（43 / 43 通过）
store 不含 axios、导出全部要求 action、标题截断与空会话复用分支存在；
ChatInput 的 Enter/Shift+Enter/isComposing/preventDefault/trim 判定/清空/disabled/v-model；
示例问题处理函数**不含发送调用**；mock 模块**无 axios/fetch/XMLHttpRequest** 且不 import api 层；
侧边栏无 token/password/jwt；三处响应式媒体查询；消息气泡 `pre-wrap`；路由未过度设计。

### 4.4 HTTP 探测
- dev server `http://localhost:5173/` → **200**，含 `id="app"` 与 `/src/main.ts`
- `/src/views/ChatView.vue`、`/src/stores/conversation.ts`、`/src/layouts/DefaultLayout.vue`、
  `/src/router/index.ts`、各组件 → 全部 **200**（Vite 转译通过）
- preview server（生产产物）→ **200**；JS 产物含全部 18 项 UI 文案与关键词；
  CSS 产物含 `--cf-accent` / `pre-wrap` / `overflow-wrap` / 三处媒体查询；
  `@media(max-width:860px)` 块确认含 `.sidebar--open{transform:translate(0);visibility:visible}`

---

## 5. 未做（严格遵守本阶段边界）

未接任何后端：Python Agent、Spring Boot API、MCP、LangGraph、RAG **全部未接**；
未实现登录 / JWT；未安装 Element Plus / Ant Design Vue / Tailwind / 任何第三方 UI 模板；
未修改后端数据库；未改后端 Git commit；未安装任何新依赖（`package.json` 零改动）。

---

## 6. 后端零改动证明

```
$ git status --porcelain campus-agent campus-service
(空)

$ git status --porcelain
?? campus-web/

$ git log --oneline -1
7c9944f refactor(agent): rebuild agent on LangGraph graph architecture
```
`campus-agent/` 与 `campus-service/` **工作区完全干净**；HEAD 仍是 Backend V1.0 那次 commit，
**未新增 commit、未 stage、未 push**。`campus-web/` 整体仍为未跟踪（与 F1 阶段结束时一致）。

---

## 7. 已知边界 / 后续

1. **未做真实浏览器截图验证**：本机 Chromium 下载两次超时（Google CDN 限速）。
   替代验证见 §4.2（真实组件 SSR 渲染 + store 行为，49 项）。等网络允许后可补 `agent-browser` 截图。
2. **会话不持久化**：刷新即清空（本阶段刻意不做 localStorage / 路由同步）。
3. **模拟回复是纯规则表**，不是真 Agent；接入时 `src/lib/mockReply.ts` 整体删除，
   换成一次 API 调用即可（组件层不用改）。
4. **两个验收脚本留在仓库内**（`scripts/verify-stage2*.mjs`）。它们只读源码与渲染，
   无副作用、不联网；如需保持仓库精简可随时删除。
5. 侧边栏底部用户区、`/chat/:conversationId` 均按「不过度设计」原则留作后续。
