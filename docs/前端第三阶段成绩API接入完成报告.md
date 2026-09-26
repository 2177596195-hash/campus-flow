# 前端第三阶段（F3-3）：成绩查询真实 API 接入完成报告

> 阶段：F3-3 成绩查询接入（Vue → Vite Proxy → Spring Boot → MySQL）
> 基线：`ec86151 feat(frontend): integrate real course API`
> 日期：2026-09-26
> 状态：**已完成并通过全量验证，未 commit（按阶段要求）**

---

## 一、改动清单（全部位于 campus-web，后端零改动）

### 新增 8 个文件

| 文件 | 职责 |
|---|---|
| `src/types/score.ts` | 成绩领域类型；契约逐字对照后端 `ScoreResponse` record |
| `src/api/scores.ts` | 成绩 API：fetch + AbortController + 超时合成 + 结构校验 + 渲染文案 |
| `src/constants/scoreIntent.ts` | 成绩意图识别 + 学期解析（只认明确写法）+ 「全部」判定 |
| `src/lib/scoreFlow.ts` | 编排层：永不抛异常、失败归一、学期追问文案（前端最小槽位） |
| `scripts/verify-f3-score-contract.mjs` | 源码契约验证（28 项） |
| `scripts/verify-f3-score-flow.mjs` | 真实后端链路验证（51 项） |
| `scripts/verify-f3-score-failure-path.mjs` | 失败路径验证（22 项） |
| `scripts/verify-f3-score-edge.mjs` | 真实浏览器端到端验证（39 项） |

### 修改 2 个文件

| 文件 | 改动 |
|---|---|
| `src/views/ChatView.vue` | 分流从三条扩为四条（成绩 → 考试 → 课程 → mock）；新增学期槽位（追问 → 下一轮补齐）；`新建/清空会话` 复位槽位；DEV 调试入口加 `__campusFlowScoreQuery`。**考试 / 课程分支代码块逐字未动**（F3-1/F3-2 契约脚本的正则全部仍命中） |
| `src/lib/mockReply.ts` | 成绩规则改为「已接通真实后端接口」兜底文案（沿 F3-1/F3-2 先例）；头部注释把成绩纳入「已分流」清单 |

### 明确未动

- **campus-service / campus-agent：零改动**（`git diff --stat HEAD -- campus-service campus-agent` 为空）
- F2 UI 架构（组件拆分 / store 结构 / 消息渲染）未动；未引入 UI 组件库、未引入新状态管理、无登录/JWT
- Python Agent 的 task_slot 未动；既有验证脚本一个未删

---

## 二、后端成绩 API 真实契约（源码 + 真实 HTTP 双重确认，非推测）

### 接口

`GET /api/scores?studentId=xxx&semester=xxx`（`ScoreController` → `@RequestMapping("/api/scores")`）

### 参数

| 参数 | 行为（实测） |
|---|---|
| `studentId` | **必填**；缺失 → 400（Spring MVC 自动，响应体 `{"status":400,...}`） |
| `semester` | **可选**（`required = false`）；不传 → 返回该学生**全部学期**成绩 |
| `semester=`（空白） | 后端 Service 归一化为「未提供」→ 与不传等价（200 + 全部成绩） |
| `semester=abc` / `2025-2026` | **后端无格式校验**：做精确匹配，匹配不到 → 200 + `[]`（不是错误） |

### semester 实际格式

**`YYYY-YYYY-N`，如 `2025-2026-1`**（学年-学期序号；种子数据含 `2025-2026-1` 与 `2024-2025-2`）。
后端对学期做**精确匹配**（`2025-2026-1` ≠ `2024-2025-2`），`ORDER BY semester DESC`（字典序=时间序）+ 同学期按课程名升序，排序全部在后端完成。

### 响应结构（原样照抄真实返回）

```json
[
  {"semester":"2025-2026-1","course":"操作系统","score":95.00,"credit":3.5},
  {"semester":"2025-2026-1","course":"数据结构","score":92.00,"credit":4.0},
  {"semester":"2025-2026-1","course":"计算机网络","score":88.00,"credit":3.0}
]
```

六个关键点：
1. **裸数组**，无 `{code,message,data}` 外层包裹（不能用 `ApiResponse`）；
2. **score / credit 是 JSON 数值**（`BigDecimal` → Jackson 数字；`DECIMAL(5,2)`/`DECIMAL(3,1)` 的十进制精确），与考试（四字段全 string）、课程（六字段全 string）**最大的契约差异**——前端类型是 `number`，`92.00` 经 `JSON.parse` 后为 `92`，渲染成「成绩：92」是数值语义下的正确显示；
3. **不泄露实体字段**：`id` / `studentId` / `createdAt` 一律不出现；
4. **查无数据 = 200 + `[]`**（该生该学期没出分是正常结果；学号不存在同样 `[]`）；
5. **学期过滤在 DB 完成**：student-001 有 2025-2026-1（3 门）与 2024-2025-2（2 门）两个学期共 5 条，`semester=2026-2027-1` → `[]`，可强断言；
6. 不同学号数据隔离（student-002 → 2 门，与 student-001 不串）。

---

## 三、前端实现要点

### 1. API 层（`src/api/scores.ts`）

- 同源相对路径 `/api/scores` + Vite 反代（与考试 / 课程一致），`fetch` + 超时与外部 `AbortSignal` 合成一路取消；
- `studentId` 由 `resolveStudentId()` 统一提供，`semester` **只从入参来**（非空字符串才进 URL，空白不发）；
- 结构校验严格：四字段、`semester/course` 为 string、`score/credit` 为**有限 number**，任何不符判 `malformed`——把「后端结构变更」变成明确失败，而不是渲染空白或误判成「暂无成绩」；
- 错误归一 `ScoreApiError{network|http|malformed}`，失败文案统一前缀「**成绩查询失败**」。

### 2. 意图与学期解析（`src/constants/scoreIntent.ts`）

- 成绩关键词：`成绩 / 分数 / 绩点 / 挂科 / 多少分 / 查分`；
- **分流顺序：成绩 → 考试 → 课程 → mock**。「看看考试成绩」「期末成绩」问的是分数不是考场安排，必须进成绩分支（若排在考试之后会被「考试」关键词劫持）；纯考试问题不含成绩词，F3-1 行为分毫不改（contract 脚本断言 score 分支位置在 exam 之前 + exam/course 分支代码块逐字原样）；
- `parseSemester` 只认两种**明确写法**：
  - 后端字面格式 `2025-2026-1`（`/(\d{4})-(\d{4})-([12])(?!\d)/`）；
  - 中文写法 `2026-2027第一学期` / `2025-2026学年第1学期` / `2025至2026学年第二学期`（年份分隔符 `-—–至到~/`，「学年/第」可省，序号 `一/二/1/2`）→ 统一转 `YYYY-YYYY-N`；
  - **其余一律 undefined**：`上学期 / 这学期 / 第一学期（缺年份）/ 2025-2026-12` 都不猜——前端没有学期日历，任何映射都是编造（与 F3-2 不猜「这周=第几周」同一策略）；
- `wantsAllSemesters`：成绩语境里明确说「全部 / 所有」= 用户的**明确选择**（不是猜测），以 `semester: undefined` 查询全部学期。

### 3. 学期槽位（前端最小方案，不碰 Python task_slot）

用户问成绩但没给学期 → **不发任何请求**，只回一句追问（`SCORE_SEMESTER_FOLLOWUP`，含格式示例与「全部」选项）。下一轮输入优先按「补学期」解释：

- 补上学期（如 `2025-2026-1`）→ 立即真实查询；
- 明确「全部」→ 以 undefined 查全部学期；
- 又问了成绩但还是没给学期 → 再追问一次；
- 换了别的问题（考试/课程/其他）→ **放弃槽位、走正常分流**（旧追问不劫持新问题）；
- `新建对话 / 清空会话` → 槽位一并复位。

### 4. 失败与空数据语义

- API 失败：统一「成绩查询失败：…」+ `error` 标记（UI「失败」标签），**绝不编造成绩**（失败路径脚本用 21 项禁词断言）；
- 空数组：明确学期 → 「已查询学号 student-001 的 2026-2027-1 学期成绩：**该学期暂无成绩记录**。」（正常回复，无失败标记）；全部 → 「暂无任何成绩记录」。

---

## 四、验证结果汇总（全部真实执行，共 305 项断言全绿）

### F3-3 新增验证

| 脚本 | 结果 | 覆盖 |
|---|---|---|
| `verify-f3-score-contract.mjs` | **28/28** | 组件不直连、分流顺序、槽位机制、数值类型契约、无硬编码、无越界 |
| `verify-f3-score-flow.mjs`（真实后端 8081） | **51/51** | 意图识别（7 个成绩问法 + 3 个负例 + 考试/课程回归）、学期解析 15 例、API 契约 A-E 全部、编排层文案与数值渲染 |
| `verify-f3-score-failure-path.mjs` | **22/22** | 不抛异常、失败前缀、error 标记、无编造、不误渲染成「暂无成绩」、store 收纳 |
| `verify-f3-score-edge.mjs`（真实 Edge + CDP） | **39/39** | 四幕浏览器交互：①没给学期→**零请求**只追问；②补 `2025-2026-1`→3 门真实成绩渲染；③中文学期+不存在学期→`semester=2026-2027-1`→200+[]→「该学期暂无成绩记录」；④「全部」→请求不带 semester→5 门跨两学期渲染 |

### 用户要求的 A–F 场景与实测结果

| 场景 | 结果 |
|---|---|
| A 有成绩 studentId+semester | 200 + 3 条（操作系统 95/3.5、数据结构 92/4、计算机网络 88/3，数值语义）✅ |
| B 无成绩（不存在学期 2026-2027-1 / 不存在学生 student-999） | 200 + `[]`，页面渲染「该学期暂无成绩记录」正常回复 ✅ |
| C 缺少 studentId | 400（后端 Spring 自动）✅ |
| D 缺少 semester | 200 + 全部 5 条（两学期，按学期从新到旧）✅；`semester=`（空白）等价于不传 ✅ |
| E 非法 semester（abc / 2025-2026） | 后端**无格式校验** → 200 + `[]`（已如实记录，不修改后端）✅ |
| F 浏览器真实链路 | Vue → Vite proxy（`acao=null` 同源生效标志）→ Spring Boot → MySQL → 页面渲染，39/39 ✅ |

### F3-1 / F3-2 回归（全部重跑）

| 脚本 | 结果 |
|---|---|
| F3-1 `verify-f3-contract.mjs` | **26/26** |
| F3-1 `verify-f3-exam-flow.mjs`（真实后端） | **9/9** |
| F3-1 `verify-f3-failure-path.mjs` | **21/21** |
| F3-2 `verify-f3-course-contract.mjs` | **19/19** |
| F3-2 `verify-f3-course-flow.mjs`（真实后端） | **34/34** |
| F3-2 `verify-f3-course-failure-path.mjs` | **22/22** |
| F3-2 `verify-f3-course-edge.mjs`（真实浏览器） | **34/34** |

**回归合计 165/165** —— 成绩分支插入后，考试与课程链路（含真实浏览器）分毫不差。

### 构建检查

- `npm run type-check`（vue-tsc --build）：**exit 0，零错误**
- `npm run build`：**exit 0**，65 modules transformed，js 118.49 kB（gzip 45.58）

---

## 五、Git 状态

- 基线确认：`ec86151 feat(frontend): integrate real course API`
- 工作区：**2 修改 + 8 新增，全部在 `campus-web/`**，存在未提交修改（按本阶段要求 **不 commit、不 push**）
- `campus-service` / `campus-agent`：**零改动**（`git diff --stat HEAD -- campus-service campus-agent` 为空）
- 验证临时件已清理（Edge 配置目录、edge 报告转储、含口令的临时启动脚本）

---

## 六、环境备注（验收时可用）

- **Spring Boot 已在 8081 运行中**（复用自本阶段验证，MySQL 已连通），可直接验收；停止：`taskkill /PID <pid> /T /F`（`netstat -ano | findstr 8081` 查 PID）
- **Vite dev server 已在 5199 运行中**（`CAMPUS_API_TARGET=http://127.0.0.1:8081`），浏览器打开 `http://localhost:5199/` 即可复现四幕交互
- 本机重启后端的坑（实测）：`$env:MYSQL_PASSWORD` 在宿主环境**传不到子进程**（被过滤）→ 需用 Spring 命令行参数
  `java -jar target\campus-service-0.1.0.jar --server.port=8081 --spring.datasource.password=<口令>`（口令勿写在对话里，走脚本文件）
- 后端接口本身**未发现任何问题**（契约与源码、实测完全一致，无需记录缺陷）
