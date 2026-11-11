# 前端第四阶段：成绩 Agent 接入完成报告（F4-1）

> 阶段：F4-1「成绩查询改走 Agent（接线 + 验证）」
> 起始 HEAD：`c63171b feat(frontend): integrate real leave API`
> 结束状态：**全部改动未提交**（按要求不 commit / 不 push），工作区见文末「git status」
> Spring Boot / MySQL / MCP / LangGraph 拓扑 / score tool / RAG：**一行未改**

---

## 一、本次范围

现场盘点确认：F4-1 的**基础设施在上一轮已完成**，缺的只有「接线 + 验证」。因此本轮**没有重做已完工的部分**：

| 已存在（本轮未改） | 位置 |
| --- | --- |
| Agent HTTP 入口 | `campus-agent/app/api/server.py`（`POST /api/agent/chat`、`GET /api/agent/health`） |
| 入口复用 CLI 运行时 | `main._build_runtime` / `main._invoke` / `main._close_runtime` |
| LangGraph 拓扑 / task_slot / score tool | `app/agent/**`（仅槽位解析补了一种写法，见 §六-1） |
| 前端 Agent 通道 | `campus-web/src/api/agent.ts`（fetch + AbortController，四类错误分类） |
| 前端 Agent 编排 | `campus-web/src/lib/agentScoreFlow.ts` |
| Vite 反代 | `vite.config.ts` 的 `/api/agent` → `AGENT_API_TARGET`（默认 `http://127.0.0.1:8000`） |
| Python 依赖 | `pyproject.toml` 的 `fastapi` / `uvicorn` |

本轮真正做的事：**ChatView 真正调用 Agent**、**threadId 会话内多轮**、**失败分类 + F3 回退**、**四层验证**（契约 / 真实 HTTP / 失败路径 / 真实浏览器）、**env 登记与文档**。

---

## 二、完整调用链（真实验证过，不是设计图）

```
浏览器（Vue ChatView）
  │  POST /api/agent/chat  {message, threadId?}      ← 同源相对路径，无 CORS 问题
  ▼
Vite dev server  :5199  ──反代 /api/agent──▶  Python Agent  :8000
                                                │
                                                ▼
                                    FastAPI  app/api/server.py
                                                │  main._invoke(runtime, message, thread_id, entrypoint="http")
                                                ▼
                                    LangGraph: intent_router → task_slot → agent ⇄ tools
                                                │
                                                ▼
                                  score tool（MCP 开启时经 MCP Server）
                                                │
                                                ▼
                              CampusApiClient  :8081  GET /api/scores?studentId=&semester=
                                                │
                                                ▼
                                        Spring Boot → MySQL
                                                │
                                                ▼
                        Agent 生成自然语言 → 原路返回 → Vue 渲染
```

实测证据（浏览器 Network 抓包，见 §五）：

- 第一轮请求体 `{"message":"帮我查一下成绩"}` —— **无 threadId**
- 第二轮请求体 `{"message":"2025-2026第一学期","threadId":"eaa85a37…"}` —— **同一个 threadId**
- 两轮都是 `http://localhost:5199/api/agent/chat`（**同源**，`acao=null`：不产生跨源请求）
- 第二轮 Agent 侧真实命中 `get_score`，页面渲染出 MySQL 真值：操作系统 95 / 数据结构 92 / 计算机网络 88

---

## 三、修改 / 新增文件

### 前端（campus-web）

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `src/views/ChatView.vue` | 改（+237/−44 区域） | 成绩分支改走 Agent；新增 `agentThreadId`、`pendingAgentSemester`、`replyWithAgentScore`、`dispatchF3Score`；失败回退 F3 |
| `src/lib/agentScoreFlow.ts` | 新增（上一轮已建，本轮最小扩展） | 返回值新增 `failureKind`，并把返回类型具名为 `AgentScoreTurn`（**签名未变**） |
| `src/api/agent.ts` | 新增（**本轮未改**） | 公共接口原样保留 |
| `.env.example` | 改 | 登记 `AGENT_API_TARGET` / `AGENT_PORT`（无任何真实密钥） |
| `scripts/verify-f4-agent-score-contract.mjs` | 新增 | 源码契约 61 条 |
| `scripts/verify-f4-agent-score-flow.mjs` | 新增 | 真实 HTTP 链路 39 条 |
| `scripts/verify-f4-agent-score-failure-path.mjs` | 新增 | 失败路径 33 条 |
| `scripts/verify-f4-agent-score-edge.mjs` | 新增 | **真实浏览器（Edge + CDP）** 35 + 10 条 |

### Agent（campus-agent）

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `app/agent/slots.py` | 改（+41/−5） | 学期槽位新增「中文写法」识别（`2025-2026第一学期`）——见 §六-1 |
| `main.py` | 改（+14/−1） | 抽出 `_apply_tracing()`；追踪写入点仍**只有 main.py 一处**——见 §六-3 |
| `app/api/server.py` | 改（未提交的新文件） | 改为调用 `main._apply_tracing()`；`app/**` 内不再出现追踪相关命名 |
| `tests/test_api_server.py` | 新增 | HTTP 入口基础契约测试 9 条（离线，不启 lifespan） |
| `tests/agent/test_task_state.py` | 改（+7） | 补中文学期写法的用例与「不猜」的反例 |

> `pyproject.toml` / `uv.lock` / `app/api/` / `run_context.py` 的改动来自上一轮（`ENTRYPOINT_HTTP` 等），本轮未再动。

---

## 四、ChatView 如何接入 Agent

### 4.1 分流顺序（frozen 既有结构，只换「谁去执行」）

```
handleSend(text)
 ├─ [F3] 成绩学期槽位（pendingScoreSemester）        ← 原样保留
 ├─ [F3] 请假信息槽位（pendingLeaveData）            ← 原样保留
 ├─ [F4] Agent 学期槽位（pendingAgentSemester）      ← 新增
 ├─ 成绩意图 → replyWithAgentScore(text)            ← 本轮改动点
 ├─ 请假意图 → F3 请假链路                           ← 未动
 ├─ 考试意图 → F3 考试链路                           ← 未动
 ├─ 课程意图 → F3 课程链路                           ← 未动
 └─ 兜底 → mockReply                                ← 未动
```

**只有成绩意图换了执行者**；考试 / 课程 / 请假三个域一个字节没动（`verify-f3-*-contract.mjs` 与浏览器用例双重把守）。

> 位置说明：F4 的槽位分支刻意排在 F3 两个槽位分支**之后**。F3-4 的契约用例按「代码里首次出现的位置」核对 F3 分支顺序（成绩 → 请假 → 考试 → 课程），F4 分支若插在前面会把 `isLeaveQuery(text)` 的首次出现挪到 `isScoreQuery(text)` 之前而使该守卫失败。这是真实发生的（一次测量失败），处理方式是**挪自己的新代码，不动 F3 的用例**。

### 4.2 Agent 成功

`runAgentScoreChat()` 返回非 error 载荷 → 直接 `appendMessage(toAgentAssistantMessage(reply))`。
成功判定只看 `reply.error !== true`，**不做任何「降级成成功」的处理**。

### 4.3 Agent 失败 → 回退 F3

```ts
pendingAgentSemester = false
if (import.meta.env.DEV) console.warn('[F4] Agent 成绩链路失败，回退 F3 直连链路', { kind, message })
stopWatch()
dispatchF3Score(text)     // = F3 原有分支逻辑原样搬进来
```

- **失败分类**来自 `runAgentScoreChat` 的 `failureKind`（`network` / `http` / `malformed` / `timeout`），不靠匹配错误文案；四类文案由 `agent.ts` 分别给出（「无法连接 Agent 服务」/「服务返回异常（HTTP xxx）」/「返回内容不是合法 JSON」/「请求超时」）。
- **回退目标健康**也被验证：Agent 停掉的同时，F3 直连链路仍然真查出 3 条成绩（脚本 `verify-f4-agent-score-failure-path.mjs` 第 2 节）。回退在真实浏览器里同样成立：Agent 请求 500 → 浏览器改打 `/api/scores` → 200 → 渲染真值，**无失败标记、无编造数据**。
- 回退后这条会话改由 F3 承担：Agent 的槽位就地放弃，F3 的 `pendingScoreSemester` 接手。
- **取消（清空会话 / 卸载页面）导致的空载荷不上屏、也不触发回退**。

### 4.4 一个刻意的例外：「全部学期」

「查我所有学期的成绩」**直接走 F3**。原因：Agent 的学期槽位只认「写出来的学年 + 学期号」（`app/agent/slots.py`），没有「全部学期」这个说法；而 F3 的接口层本来就支持「不带 `semester` = 查全部」。把这句话交给能回答它的链路，比让 Agent 反复追问更诚实——该分支**没有新增行为**，就是 F3 原本的做法。

---

## 五、threadId 如何保存

| 要求 | 实现 |
| --- | --- |
| 第一次不传 | `runAgentScoreChat(text, agentThreadId ?? undefined, signal)`；`agent.ts` 里 `threadId: threadId || undefined`，`JSON.stringify` 直接省略该键（实测请求体只有 `{"message":…}`） |
| 回传后保存 | `if (threadId) agentThreadId = threadId` |
| 下一轮复用 | 槽位分支与成绩分支都从 `agentThreadId` 取值 |
| 新建 / 清空会话清空 | `handleNewConversation` / `handleClearConversation` 显式置 `null` |
| 侧边栏切会话清空 | `watch(currentConversationId)` 复位（**必须忽略 `previous === null` 那一次**，见 §六-2） |
| 不用全局变量 | 组件内 `let agentThreadId: string \| null = null`（不挂 `window`；仅在 DEV 下暴露一个只读取值函数供控制台核对） |
| 不做复杂持久化 | 无 localStorage / sessionStorage / indexedDB；刷新页面即重置 |
| 未复用 Pinia 会话 id | 评估过：store 的会话 id 是**前端本地**标识且 store 层明确「不发任何请求」，把它当 threadId 会把两套生命周期绑在一起。故采用最小改动：组件内 `let` + 会话切换复位 |

---

## 六、过程中发现并修复的 3 个真实问题

这三条都是**靠真实链路 / 真实浏览器**才暴露的，不是设计时能想到的。

### 六-1（Agent 侧）中文写法的学期补不上槽位 → 多轮第二轮拿不到成绩

- **现象**：第一轮「帮我查一下成绩」正常反问学期；第二轮带同一 threadId 回复「2025-2026第一学期」，`intent` 变成 `unknown`、`toolCalls` 为空，**又反问了一遍学期**。
- **根因**：`app/agent/slots.py::resolve_semester` 只认字面量 `2025-2026-1` 与「本学期 / 上学期」关键词；中文写法在中文学期正则在**前端**有（`scoreIntent.ts`），Python 侧没有。
- **修复**：新增 `SEMESTER_CHINESE_PATTERN`（要求两个 4 位年份 + 明确的学期号），归一化成后端格式；学年不连续、没有年份的「大二第一学期」仍然判不出（**不猜**，与既有口径一致）。补了 7 条用例（`tests/agent/test_task_state.py`）。
- **验证**：第二轮真实命中 `get_score` 并返回真值；`resolve_semester("2025-2030第一学期") is None`。

### 六-2（前端）会话 watcher 把刚挂上的槽位清掉了 → 第二轮掉进 mock 回复

- **现象**：浏览器里第一轮正常（Agent 反问了学期），第二轮「2025-2026第一学期」**一个请求都没发**，页面出现「（本地占位回复，尚未接入 Agent）」。
- **根因**：为了「切会话就复位 threadId」，我 watch 了 `currentConversationId`；而**本条消息自己的第一条消息**会走 `store.addMessage → upsertCurrentConversation`，把 `currentConversationId` 从 `null` 变成新 id，watcher 于是把 `handleSend` 里刚设置的 `pendingAgentSemester` 复位成 `false`。
- **修复**：watcher 忽略 `previous === null` 那一次变化，并加了显式注释说明这一坑；契约脚本新增一条守卫（`the "first message creates the conversation" transition must NOT reset F4 state`）。
- **验证**：浏览器第二轮请求体带上同一 threadId，页面渲染真值；`npm run type-check` / `build` 通过。

### 六-3（Agent 侧）新 HTTP 入口违反「追踪写入点只有一个」架构守卫

- **发现**：`pytest` 全量跑出 `tests/test_langsmith_tracing.py::test_application_code_has_no_langsmith_usage` 失败——`app/api/server.py` 里出现了追踪相关的名字（守卫扫描 `app/**`，`main.py` 在仓库根因此不在扫描范围）。
- **修复**：把落地动作收敛回 `main.py`（新增 `_apply_tracing()` 包装，`main()` 内部改调它），`server.py` 改为 `from main import _apply_tracing, …`，并清掉文件里的相关措辞；`main.py` 内 `apply_langsmith_tracing()` 的出现次数仍为 **1**（守卫生效）。
- **验证**：`pytest` 全量 **1022 passed / 0 failed**；LangSmith 守卫用例通过。

---

## 七、验证结果（全部为真实执行结果）

### 7.1 前端契约 / 链路 / 失败路径（Node + Vite SSR 真实模块）

| 脚本 | 结果 |
| --- | --- |
| `verify-f4-agent-score-contract.mjs` | **61 / 61** |
| `verify-f4-agent-score-flow.mjs` | **39 / 39** |
| `verify-f4-agent-score-failure-path.mjs` | **33 / 33** |

失败路径脚本覆盖的六种坏情况：`network`（端口无人监听）、`malformed`（200 但非 JSON）、`malformed`（200 但字段不符契约）、`http`（500 带 `detail`）、`http`（503 无 JSON 体）、`timeout`（连上但永不回包，实测 15010ms 触发客户端 15s 截止）。四种分类全部产出且互不混淆。

### 7.2 真实浏览器（Edge 153 headless + CDP，Network 级抓包）

| 场景 | 结果 |
| --- | --- |
| Agent 在线（`verify-f4-agent-score-edge.mjs … up`） | **35 / 35** |
| Agent 掉线、回退 F3（`… down`） | **10 / 10** |

覆盖：同源代理放行、首轮不带 threadId、第二轮同 threadId、真实成绩渲染、非失败/非占位标记、考试与课程仍走 `/api/exams`·`/api/courses`、清空会话后 threadId 复位（新会话 id 与旧的不同）、控制台零错误。

### 7.3 F3 / F2 回归

| 域 | 脚本结果 |
| --- | --- |
| F3-1 考试 | contract 26/26 · exam-flow 9/9 · failure-path 21/21 · **edge 15/15** |
| F3-2 课程 | contract 19/19 · flow 34/34 · failure-path 22/22 · **edge 34/34** |
| F3-3 成绩 | contract 28/28 · flow 51/51 · failure-path 22/22 · **edge 31/39（8 条断言已被 F4-1 有意取代，见 §八-1）** |
| F3-4 请假 | contract 40/40 · flow 59/59 · failure-path 22/22 |
| F2 骨架 | `verify-stage2.mjs` / `verify-stage2-render.mjs` 均 exit 0 |

### 7.4 前端工程检查

- `npm run type-check`（`vue-tsc --build`）：**通过**（注：项目里没有 `typecheck` 脚本，实际脚本名是 `type-check`）
- `npm run build`：**通过**，`dist/assets/index-*.js` 128.05 kB（gzip 47.79 kB）

### 7.5 Python 侧

| 项 | 结果 |
| --- | --- |
| 全量 `pytest` | **1022 passed / 0 failed**（120s） |
| 新增 `tests/test_api_server.py` | 9 passed（健康检查两态、契约字段、threadId 生成/延续、422 空消息、503 未就绪、500 不泄露内部细节、`entrypoint="http"`） |
| `tests/agent/test_task_state.py` | 66 passed（含新增的中文学期用例） |
| 真实 HTTP 链路 | `GET /api/agent/health` → 200 `{"status":"ok"}`；`POST /api/agent/chat` 单轮 / 多轮 / 带学期 / 空结果 全部 200 且数据为 MySQL 真值 |

---

## 八、已知限制

1. **`verify-f3-score-edge.mjs` 有 8 条断言已被 F4-1 有意取代**（该脚本**未修改**，按要求不删改 F3 测试）：
   它的前提是「浏览器自己去打 `/api/scores`」，而 F4-1 之后成绩问题由 Agent 在**服务端**调用 Spring Boot，浏览器只发 `/api/agent/chat`。逐条：
   - `#1 follow-up mentions the 全部 option` —— 追问文案现在由 Agent 生成，不提供「全部」；
   - `#2 request carries studentId AND semester=2025-2026-1` / `#2 browser got HTTP 200 from the real backend` —— 浏览器侧不再出现 `/api/scores` 请求；
   - `#2 count 共 3 门` / `#2 NUMERIC semantics 成绩：95` —— F3 的固定文案被 Agent 的表格替代；
   - `#3 semester=2026-2027-1 in the URL` / `#3 HTTP 200` —— 同上；
   - `#3 该学期暂无成绩记录` —— Agent 说的是「2026-2027-1 学期暂时查询不到成绩记录（教务系统实时查询返回为空）」，**语义正确但措辞不同**。
   同一片能力的**新期望**已由 `verify-f4-agent-score-edge.mjs`（35/35 + 10/10）承担。
2. **Agent 侧不支持「全部学期」说法**：前端把「全部 / 所有学期」路由到 F3（§4.4）。Agent 收到「全部」会继续追问学期。
3. **Agent 回复由模型生成，措辞不确定**：因此浏览器与链路断言只锚定语义与关键数据（真值 95/92/88、是否 error、是否占位），不锚定固定句式——F3 那种「共 3 门」的字面量断言在 Agent 链路上不成立。
4. **threadId 只在本 ChatView 会话生命周期内有效**：无持久化，刷新页面即重置；`CHECKPOINTER_BACKEND=memory` 时 Agent 进程重启后旧 threadId 的会话状态也消失（表现为重新追问学期）。
5. **两条成绩链路并存**：成绩域现在有「Agent 优先 + F3 回退」两条路径，属于阶段内的有意过渡；F3 的 `scoreFlow.ts` / `scores.ts` / `scoreIntent.ts` 全部保留，未删。
6. **生产环境仍需网关**：`/api/agent` 与 `/api/*` 的同源暴露依赖 Vite 反代，生产要由网关 / 反向代理承担（与 F3 同款结论，未变）。
7. **未做且刻意未做**：考试 / 课程 / 请假的 Agent 化、RAG 接入、登录鉴权、UI 改版、Spring Boot 与数据库改动。
8. **F4 全部改动未提交**（工作区含未跟踪文件），按指令不 commit / 不 push。

---

## 九、复现命令

```powershell
# 1) Spring Boot（本机 8080 被占，用 8081）+ Agent + Vite
#    Agent：
cd D:\code\campus-flow\campus-agent
$env:PYTHONPATH='D:\code\campus-flow\campus-agent'; $env:AGENT_PORT='8000'
D:\code\campus-flow\.venv\Scripts\python.exe -m app.api.server

#    Vite（反代：/api/agent → 8000，其余 /api → 8081）
cd D:\code\campus-flow\campus-web
$env:CAMPUS_API_TARGET='http://127.0.0.1:8081'; node node_modules\vite\bin\vite.js --port 5199 --strictPort

# 2) 验证
cd D:\code\campus-flow\campus-web
node scripts\verify-f4-agent-score-contract.mjs
node scripts\verify-f4-agent-score-flow.mjs                              # 可用 BASE_URL / AGENT_BASE_URL 覆盖
node scripts\verify-f4-agent-score-failure-path.mjs                      # 含 15s 超时用例
node scripts\verify-f4-agent-score-edge.mjs http://localhost:5199/ up up
node scripts\verify-f4-agent-score-edge.mjs http://localhost:5199/ down down   # 需先停掉 Agent

# 3) F3 / F2 回归（BASE_URL 指向真实后端）
$env:BASE_URL='http://localhost:8081'
node scripts\verify-f3-contract.mjs; node scripts\verify-f3-exam-flow.mjs; …

# 4) Python
cd D:\code\campus-flow\campus-agent
$env:CODEBUDDY_SAFE_DELETE_ENABLED='0'
D:\code\campus-flow\.venv\Scripts\python.exe -m pytest -q
```

---

## 十、git status（结束时）

```
 M campus-agent/app/agent/run_context.py      ← 上一轮
 M campus-agent/app/agent/slots.py            ← 本轮（中文学期）
 M campus-agent/main.py                       ← 本轮（_apply_tracing）
 M campus-agent/tests/agent/test_task_state.py ← 本轮（用例）
 M campus-web/.env.example                    ← 本轮（AGENT_API_TARGET / AGENT_PORT）
 M campus-web/src/views/ChatView.vue          ← 本轮（接线）
 M campus-web/vite.config.ts                  ← 上一轮
 M pyproject.toml / uv.lock                   ← 上一轮
?? campus-agent/app/api/                      ← 上一轮（HTTP 入口）
?? campus-agent/tests/test_api_server.py      ← 本轮（新增用例）
?? campus-web/src/api/agent.ts                ← 上一轮
?? campus-web/src/lib/agentScoreFlow.ts       ← 上一轮 + 本轮最小扩展
?? campus-web/scripts/verify-f4-agent-score-{contract,flow,failure-path,edge}.mjs  ← 本轮
?? campus-web/.f4-agent-edge-{final-up,down-run}.txt  ← 本轮浏览器抓包的原始证据（开发期产物）
?? docs/前端第四阶段成绩Agent接入完成报告.md        ← 本文档
```

HEAD 仍为 `c63171b`，**未产生任何提交**。

临时探针（`.tmp-*`）与浏览器 profile 目录（`.edge-profile*`）已在收尾时清除；上面两个
`.f4-agent-edge-*.txt` 是 **Edge + CDP 的抓包原文**（UP 模式 / Agent 掉线模式各一份），
保留作为浏览器侧结论的原始凭证，可随时删除。
