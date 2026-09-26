# 前端 F3 第一阶段执行报告：接入真实校园 API

> 日期：2026-09-26 · 仓库 `D:\code\campus-flow`
> 基线：后端 V1.0 `7c9944f` / 前端 F2 `fc417bd`
> 范围：`GET /api/exams?studentId=...` 单接口打通，前端聊天区渲染真实数据
> **未 commit、未 push。`campus-service` 与 `campus-agent` 零修改（`git diff 7c9944f -- campus-service campus-agent` 输出为空）。**

---

## 一、结论速览

| 验收项 | 结果 |
|---|---|
| `npm run type-check` | **exit=0**，零 TS 报错 |
| `npm run build` | **exit=0**，`59 modules transformed`，`index.js 111.52 kB / gzip 44.31 kB` |
| 实际调用 `GET /api/exams?studentId=student-001` | **成功**，HTTP 200，返回 2 条真实数据 |
| 成功响应进入前端聊天区 | **成功**（真实浏览器 Edge + CDP 驱动，**15/15 PASS**） |
| 接口失败不产生假数据 | **成功**（真实浏览器 + 模块级双层验证，**21/21 PASS**） |
| 后端是否零修改 | **是**，后端目录 `git diff` 为空，HEAD 未动 |

**一句话**：前端已能真实调用考试接口并在聊天区渲染真实考试安排；后端一行未改。

---

## 二、真实 API 返回结构（读后端源码确认，未猜）

**来源**：`campus-service/src/main/java/edu/ylu/campus/controller/ExamController.java`
与 `dto/ExamScheduleResponse.java`

```java
@RestController
@RequestMapping("/api/exams")
public class ExamController {
    @GetMapping
    public List<ExamScheduleResponse> listExams(@RequestParam("studentId") String studentId) { ... }
}

public record ExamScheduleResponse(String course, String date, String time, String room) {}
```

**实测响应**（`GET /api/exams?studentId=student-001` → 200）：

```json
[
  { "course": "计算机网络", "date": "2026-09-28", "time": "14:00", "room": "A302" },
  { "course": "数据结构",   "date": "2026-09-30", "time": "19:00", "room": "B201" }
]
```

四个必须记住的契约事实（都已落到前端类型定义与注释里）：

1. **顶层就是数组，没有外层包裹**（不是 `{code, message, data}`）→ 因此**不能复用** `types/api.ts` 的 `ApiResponse`。
2. **四个字段全是字符串**：`course` / `date` / `time` / `room`。日期字段名是 `date`，**不是** `examDate`。
3. **不泄露实体字段**：`id` / `studentId` / `createdAt` / `examDate` 一律不出现在响应里。
4. **查无此人 = 200 + `[]`**（不是 404）——空数组是**正常结果**，不是失败。

补充实测（经 Vite 代理验证，见 §四）：

| 请求 | 状态 | 响应体 |
|---|---|---|
| `?studentId=student-001` | 200 | 计算机网络 2026-09-28 14:00 A302；数据结构 2026-09-30 19:00 B201 |
| `?studentId=student-002` | 200 | 大学英语 2026-09-29 09:00 C105（**数据确实随学号变化**） |
| `?studentId=unknown-xyz` | 200 | `[]` |
| 缺 `studentId` | 400 | Spring Boot 标准错误体 |

---

## 三、前端怎么调用

### 3.1 分层（沿用 F2 结构，未做大重构）

```
ChatView.vue            只做分支：isExamQuery(text) → replyWithExams()，否则 replyWithMock()
  └─ lib/examFlow.ts    编排层：runExamQuery() 永不抛异常，失败也返回一条回复
       └─ api/exams.ts  请求层：fetchExamSchedules() + 严格结构校验 + 错误归一
            └─ config/env.ts   唯一读 import.meta.env 的地方
```

### 3.2 新增文件（8 个）

| 文件 | 职责 |
|---|---|
| `src/types/exam.ts` | `ExamSchedule` / `ExamQuery` / `ExamApiErrorKind`；注释里标注契约来源为后端 DTO 文件名 |
| `src/config/env.ts` | `API_BASE_URL` / `CAMPUS_STUDENT_ID` / `resolveStudentId()` / `buildApiUrl()`；**唯一**读环境变量处 |
| `src/api/exams.ts` | `EXAMS_PATH` / `ExamApiError` / `fetchExamSchedules()` / `formatExamSchedules()` |
| `src/constants/examIntent.ts` | `isExamQuery()` 意图判定（**只管判定，不管跳转**，符合项目约定） |
| `src/lib/examFlow.ts` | `runExamQuery()` 编排，返回 `ExamReplyPayload`；**承诺不抛异常** |
| `scripts/stub-exams-server.mjs` | 按真实契约应答的桩服务（端口 8099，缺 `studentId` 返 400） |
| `scripts/verify-f3-exam-flow.mjs` | SSR 加载真实模块 + 真实 HTTP，成功/失败双路径 |
| `scripts/verify-f3-contract.mjs` | 26 条源码契约断言（组件不直连 axios、考试不走 mock 等） |
| `scripts/verify-f3-edge.mjs` | Edge + CDP 真实浏览器端到端 |
| `scripts/verify-f3-failure-path.mjs` | 失败路径专项（不抛异常 / 无编造数据 / 正确进 store） |

### 3.3 修改文件（9 个）

| 文件 | 改动 |
|---|---|
| `vite.config.ts` | **新增 `server.proxy`：`/api` → `CAMPUS_API_TARGET`（默认 `http://127.0.0.1:8080`）** |
| `src/config/env.ts` | `API_BASE_URL` 默认从 `http://localhost:8080` 改为**空串**（走同源相对路径） |
| `src/api/exams.ts` | `mode` 随 `API_BASE_URL` 在 `same-origin` / `cors` 间切换；文件头记录 CORS 踩坑结论 |
| `src/api/index.ts` | 注释说明业务 API **刻意不挂 Axios barrel**（避免隐式耦合） |
| `src/types/index.ts` | re-export `./exam` |
| `src/types/conversation.ts` | `ChatMessage` 增加 `error?: boolean` |
| `src/stores/conversation.ts` | `addMessage(role, content, mock, error)` + 新增 `appendMessage()` |
| `src/views/ChatView.vue` | 意图分流 + `AbortController` + 清空/卸载时取消；`__campusFlowExamQuery` 仅 DEV |
| `src/components/chat/ChatMessage.vue` | `error` prop + 「失败」标签（`v-else-if` 优先级高于「占位」） |
| `src/lib/mockReply.ts` | 考试规则改为「已走真实后端」的兜底说明 |
| `.env.example` | 文档化同源代理方案与 `CAMPUS_API_TARGET` 覆盖方式 |

**组件不直接调用 axios（有断言）**；`stores/conversation.ts` **仍零网络**（有断言）。

### 3.4 开发期配置

```bash
# campus-web/.env（已被 .gitignore:28 忽略，只提交 .env.example）
VITE_API_BASE_URL=          # 留空 → 走同源相对路径 /api/exams
VITE_CAMPUS_STUDENT_ID=student-001

# 后端换端口时（不要改 vite.config.ts 默认值）：
$env:CAMPUS_API_TARGET='http://127.0.0.1:8091'; npm run dev
```

`VITE_API_BASE_URL` 之所以**留空**而不是填绝对 URL：浏览器直连后端会被 CORS 拦下（见 §五）。

---

## 四、成功 / 失败分别怎么处理

### 成功路径

真实浏览器实测（Edge 153 headless + CDP，经 Vite 代理）：

```
请求：GET http://localhost:5199/api/exams?studentId=student-001   （同源）
响应：200

聊天区渲染：
已查询学号 student-001 的考试安排，共 2 场：

1. 计算机网络｜2026-09-28 14:00｜考场：A302
2. 数据结构｜2026-09-30 19:00｜考场：B201
```

无「占位」标签、无「失败」标签、控制台零错误。

**空数组**（`student-002` 之外的未知学号）走**正常回复**：`已查询学号 X 的考试安排：当前没有待参加的考试。`
——刻意与失败区分，**绝不把「结构不对」误判成「该生没考试」**。

### 失败路径

`ExamApiError` 三分类，`message` 已是可直接展示的中文，统一前缀 **「考试安排查询失败」**：

| kind | 触发条件 | 文案 |
|---|---|---|
| `network` | 连不上 / 超时（15s） | 考试安排查询失败：无法连接后端服务，请确认服务已启动且地址正确 |
| `http` | 非 2xx | 考试安排查询失败：服务返回异常状态（HTTP 404） |
| `malformed` | 非 JSON / 字段不符 | 考试安排查询失败：返回数据不是考试安排列表 ／ 返回数据字段与后端约定不一致 |

**三个保证**：

1. `runExamQuery()` **永不抛异常** —— 错误在编排层转成一条 assistant 回复，页面不会白屏；
2. 失败的回复**带 `error` 标记** → 聊天区显示红色「失败」标签；
3. 失败时**绝不编造考试数据** —— 有专项断言逐一排除课程名 / 考场号 / 日期 / 时间的泄漏。

---

## 五、关键取舍：CORS 与同源代理（本阶段最重要的一条经验）

**问题**：后端只监听回环地址、且**没有任何 CORS 配置**（不改后端是硬约束）。浏览器直连 `http://localhost:8091` 时：

```
Network.requestWillBeSent  →  http://localhost:8091/api/exams?studentId=student-001
Network.loadingFailed      →  net::ERR_FAILED          ← 无 Access-Control-Allow-Origin
```

**踩过的坑（两次假设都被实测推翻，记录备查）**：

1. ~~Axios 直连~~ → 浏览器对 XHR 跨源请求强制走 CORS，无 ACAO 必失败；
2. ~~`fetch` + `mode:'cors'` 简单请求能免检~~ → **真实浏览器实测同样 `net::ERR_FAILED`**，直连方案被证伪。
   （此前模块级验证里 fetch 看着成功，是因为验证进程在 Node 侧运行、**没有浏览器同源策略**——这是本轮最有价值的一课：
   **Node 侧 HTTP 成功 ≠ 浏览器能拿到响应**。）

**最终方案**：**同源相对路径 + Vite 开发服务器反代**。

```ts
// vite.config.ts
const CAMPUS_API_TARGET = process.env.CAMPUS_API_TARGET ?? 'http://127.0.0.1:8080'
server: { proxy: { '/api': { target: CAMPUS_API_TARGET, changeOrigin: true } } }
```

浏览器只请求 `/api/exams?...`（与页面同源，**不存在跨源**），由开发服务器在 Node 侧转发。

**两个刻意设计**：

- 代理目标读 `CAMPUS_API_TARGET` 而**不读 `VITE_API_BASE_URL`**：后者是给浏览器代码看的，把两者绑在一起会导致「一改就把两边同时改坏」，概念必须分开；且 `CAMPUS_API_TARGET` **不进入浏览器包**。
- 保留 `fetch` 而非换回 Axios：本模块需要「超时 + 外部 `AbortSignal` 合成一路取消」，原生 `fetch` + `AbortController` 表达最直接。**对外签名与错误类型不变**，将来换实现调用方无需改动。

生产环境需由网关 / 反向代理承担同样的同源暴露职责（或后端加 CORS）。

---

## 六、验证证据（全部实际执行）

| 验证 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `vue-tsc --build` | **exit=0** |
| 生产构建 | `vite build` | **exit=0**，59 modules |
| 源码契约 | `node scripts/verify-f3-contract.mjs` | **26/26 PASS** |
| 链路（成功，桩） | `BASE_URL=http://localhost:8099 node scripts/verify-f3-exam-flow.mjs` | **9/9 PASS** |
| 失败路径 | `node scripts/verify-f3-failure-path.mjs` | **21/21 PASS** |
| **真实浏览器端到端** | `node scripts/verify-f3-edge.mjs http://localhost:5199/ proxy` | **15/15 PASS** |
| 代理正确性 | 四组请求探针（见 §二表格） | 全部符合契约 |

**真实浏览器 15/15 的关键断言**：

```
[PASS] a real XHR/fetch to /api/exams was issued by the page
       >> http://localhost:5199/api/exams?studentId=student-001
[PASS] no request-level failure (CORS/network) >> []
[PASS] browser got HTTP 200 from the real backend >> status=200, acao=null
[PASS] page rendered real course 计算机网络
[PASS] page rendered real room A302 / date 2026-09-28 / time 14:00
[PASS] page rendered second real course 数据结构 / room B201
[PASS] no 占位 tag on the real answer >> tag=null
[PASS] no 失败 tag on the real answer >> tag=null
```

> 注：`acao=null` 是**正常的** —— 同源请求不需要 `Access-Control-Allow-Origin`，这正是本方案生效的标志。

**后端零修改证据**：

```bash
$ git diff --stat 7c9944f -- campus-service campus-agent
   (输出为空)
$ git log --oneline -1
fc417bd feat(frontend): complete CampusFlow F2 UI skeleton
```

---

## 七、环境备注（本机，后续阶段可复用）

- **8080 被 `steamwebhelper.exe` 占用** → 真实联调用 `SERVER_PORT=8091`。
- 后端启动：`$env:MYSQL_PASSWORD='123456'; $env:SERVER_PORT='8091'; java -jar target/campus-service-0.1.0.jar`
  → Hikari `campus-pool - Start completed`。
- 本机**无 Chrome，但有 Edge**（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`），同属 Chromium，可用 `--remote-debugging-port` + CDP 驱动。
- **`Start-Process` 启 dev server 会立刻退出、`setsid` 在 Git Bash 不存在、Bash 后台任务在工具调用返回后被杀** —— 可靠姿势是 **Bash 工具的 `run_in_background=true`**。
- **Node 侧 HTTP 成功不能推出浏览器端成功**（本轮核心教训，见 §五）。

---

## 八、未做 / 边界（严格按范围）

**未做**：Python Agent / LangGraph / MCP / RAG 全未接入；无登录、无 JWT；未引入 UI 组件库；未新增任何 npm 依赖（`package.json` 零改动）；未加 `/chat/:conversationId`；未提前设计后端数据库模型；未改任何后端代码。

**已知边界**：
- `studentId` 暂时固定为 `student-001`（前端开发期配置，无登录体系），代码注释已标明「接入认证后应改为从登录态读取」。
- `isExamQuery()` 是**临时关键词规则**（刻意避开裸「安排」以防「安排一下课表」误判），接入 Agent 后整文件删除即可。
- 同源代理是**开发期**方案；生产需网关/反向代理或后端加 CORS。

**遗留件**：`scripts/` 下 5 个 F3 验证脚手架 + 1 个桩服务留在仓库内（只读、无副作用，与 F2 同类脚本一致）。若不希望入库，建议加入 `.gitignore` 或删除。
`.env` 已被忽略，只提交 `.env.example`。

**Git**：未 commit、未 push。当前工作区为 9 个 `M` + 10 个 `??`，全部在 `campus-web/` 内。
