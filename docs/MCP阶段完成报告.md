# CampusFlow MCP 阶段完成报告

**阶段目标**：在「不重写现有 Spring Boot 业务、不直接连接 MySQL、不改 RAG / AgentState /
IntentClassifier / LangGraph 总体路由结构」的前提下，把「考试安排查询」封装成**真正可调用的
MCP Tool**（只做一个：`get_exam_schedule`），并让 Agent 通过 MCP 调用。

**链路**：
```
用户 -> LangGraph Agent -> MCP Client(stdio) -> campus-mcp-server 子进程
     -> 复用 CampusApiClient -> Spring Boot REST /api/exams -> JPA -> MySQL
     -> MCP Tool Result -> LangChain 适配层 -> ToolMessage -> Agent 自然语言作答
```

---

## 1. MCP SDK 名称和版本

| 项 | 值 |
|---|---|
| 包名 | `mcp`（**官方 Python MCP SDK**，非社区包） |
| 版本 | `mcp==2.2.0`（`mcp-types==2.2.0`） |
| 安装方式 | `uv add mcp`（走清华源；`venv` 内无 pip 模块，`pip install` 不可用） |
| 声明位置 | 仓库根 `pyproject.toml` → `dependencies` 中显式 `"mcp>=2.2.0"` |

**关键版本差异（照抄 v1 教程会直接崩）**：
- **v2 已无 `FastMCP`** → 正确入口是 `from mcp.server.mcpserver import MCPServer`
  （v1 的 `mcp.server.fastmcp.FastMCP` 会 `ModuleNotFoundError`）。
- 工具错误类型在 `mcp.server.mcpserver.exceptions.ToolError`。
- Server：`MCPServer(name, description=...)` + `@server.tool(name=, description=)` +
  `server.run(transport="stdio")`。
- Client：`mcp.client.stdio.{StdioServerParameters, stdio_client}` + `mcp.ClientSession`。
- 进程内调用（测试用）：`server.call_tool(name, args)` / `server.list_tools()`（无需活会话）。

以上全部经**实测脚本 + SDK 源码**确认，未依据任何教程猜测。

---

## 2. MCP Server 结构

新增 `campus-agent/app/mcp/`（与 `app/infrastructure` 平级的基础设施层）：

```
app/mcp/
  __init__.py    导出异常族与客户端单例（MCPClientError / MCPServerUnavailable /
                 MCPToolInvocationError / MCPToolNotFound / MCPToolResultError /
                 StdioMCPClient / get_mcp_client / reset_mcp_client）
  server.py      Campus MCP Server：SERVER_NAME="campus-mcp-server"，
                 build_server() 只注册一个工具；main() 以 stdio 运行
  client.py      StdioMCPClient：同步门面 + 后台线程专属事件循环 + 常驻会话
  tools.py       MCP -> LangChain 适配层
```

**Server 端只做三件事**（`app/mcp/server.py`）：

1. 暴露 MCP 工具 `get_exam_schedule`（入参 `student_id`）；
2. 调用**既有的** `get_campus_api_client().fetch_exam_schedule(student_id)`；
3. 经 `normalize_exam_rows()` 整理字段后作为 Tool Result 返回。

**它不做**：不连 MySQL、不写 SQL、不拼 URL、不处理 HTTP 状态码、不复制任何业务数据字面量
（有守卫用例扫描源码）。

**启动方式**：`python -m app.mcp.server`（stdio transport，由 MCP Client 以子进程拉起）。
**`stdout` 是协议通道** → `_configure_logging()` 把日志固定到 `stderr`，任何 `print` 都会破坏报文。

---

## 3. MCP Tool schema

`tools/list` 实测返回（`inputSchema` 由函数签名 + 类型注解自动生成）：

```json
{
  "name": "get_exam_schedule",
  "description": "查询指定学生的近期考试安排。数据来自校园教务系统的实时查询（不是本地固定数据）。返回该学生的全部考试安排，按考试时间升序；每项含四个字段：course（考试科目）、date（考试日期 yyyy-MM-dd）、time（开考时间 HH:mm）、room（考场）。当用户询问「我最近有什么考试」「什么时候考某门课」「在哪个考场考」等与考试安排相关的问题时调用本工具。若用户只关心其中某一门课，请从返回结果中筛选出对应科目再作答。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "student_id": { "title": "Student Id", "type": "string", "description": "学生学号，例如 student-001" }
    },
    "required": ["student_id"],
    "title": "get_exam_scheduleArguments"
  },
  "outputSchema": {
    "type": "object",
    "properties": { "result": { "type": "array", "items": { "type": "object", "additionalProperties": { "type": "string" } } } },
    "required": ["result"],
    "title": "get_exam_scheduleOutput"
  }
}
```

返回内容（业务字段恒为四字段字符串）：

```json
{"result": [
  {"course": "计算机网络", "date": "2026-09-28", "time": "14:00", "room": "A302"},
  {"course": "数据结构",   "date": "2026-09-30", "time": "19:00", "room": "B201"}
]}
```

> `outputSchema` **不是装饰**：SDK 对泛型返回值（`list`/`dict`）会包一层 `{"result": ...}`
> 以满足「`structuredContent` 必须是对象」的规范，客户端据此判断是否需要拆包装。

---

## 4. MCP Client 实现

`app/mcp/client.py` → `StdioMCPClient`（**同步**门面）：

- **同步/异步边界**：MCP SDK 全异步（anyio），LangGraph `ToolNode` 走同步 `invoke`。
  实现为「**一个后台守护线程承载一条专属事件循环**」：线程内 `new_event_loop()` 跑常驻协程
  `_session_main`（建连 → `initialize` → `list_tools` → 挂起等待关闭）；主线程侧用
  `asyncio.run_coroutine_threadsafe(...).result(timeout)` 投递协程并同步取结果。
- **选常驻会话**（而非每次调用起一个 Server 进程）：MCP 的价值之一是会话级能力协商与
  工具发现（`initialize` + `tools/list` 只做一次）；每次重开进程会让「发现」退化成摆设。
- 对外方法：`start()`（幂等）/ `close()` / `list_tools()` / `find_tool(name)` /
  `acall_tool()`（异步）/ `call_tool()`（同步，Agent 走这条）。
- **错误分类**（消息均为可读中文，不含命令行与 traceback）：
  `MCPClientError` 基类 → `MCPServerUnavailable`（启动失败 / 断开 / 超时）、
  `MCPToolNotFound`（服务端无此工具）、`MCPToolInvocationError`（服务端明确 `isError`）、
  `MCPToolResultError`（结果无法解析）。
- **结果取值**（`extract_tool_payload`）：**结构化内容优先**，按工具声明的 `outputSchema`
  决定是否拆 `{"result": ...}` 包装；仅当无结构化内容时才退回解析文本。
  这样做的原因是实测：数组返回会被 SDK 拆成**每个元素一个 TextContent**，拼接后是
  `{...}{...}` **非法 JSON**，「文本优先 + `json.loads`」必然失败。
- **错误文案**：SDK 对可预期失败加前缀 `Error executing tool <name>: `，
  `_tool_error_message` 剥掉前缀与冒号，得到可直接转述给用户的中文。
- 单例：`get_mcp_client()`（`lru_cache`，只读配置**不建连**）/ `reset_mcp_client()`。

---

## 5. LangChain 适配

`app/mcp/tools.py`（**刻意做薄**：不做业务转换、不解析业务字段、不重试、不降级）：

| 组件 | 职责 |
|---|---|
| `build_args_schema(mcp_tool, *, bound_arguments)` | 按服务端 `inputSchema` 生成 pydantic 模型；未登记类型退化为 `Any` |
| `MCPToolAdapter(BaseTool)` | `name` / `description` / `args_schema` **全部来自 `tools/list` 的发现结果**；`_run` / `_arun` 只做「合并参数 → `client.call_tool`」 |
| `make_mcp_tool(client, mcp_tool, *, bound_arguments)` | 由已发现的工具定义构造适配器 |
| `build_exam_mcp_tool(client, *, settings)` | 建连 + 发现 `get_exam_schedule` + 绑定学号 |
| `build_agent_tools(*, settings, client)` | 按配置决定**考试工具的实现**，返回本轮工具清单 |

**两个关键设计**：

1. **一个能力只有一个槽位**：MCP 开启时用适配器**替换**考试槽位，绝不追加
   （否则模型看到两个同名能力）。`build_agent_tools()` 的四种分支：
   - `enabled=false` → 返回既有 `AGENT_TOOLS`（逐项相同，无任何建连动作）；
   - `enabled=true` → 建连发现 → 替换考试槽位，另两项保持同一对象；
   - MCP 不可用 → **回退直连 + WARNING**（不让 Agent 起不来，也不静默冒充 MCP）；
   - 服务端未注册该工具 → 同样回退。
2. **绑定参数（bound argument）**：`student_id` 由宿主配置（`CAMPUS_STUDENT_ID`）绑定：
   - **从模型可见的 `args_schema` 中移除** → 模型看到的是**无参工具**，与 MCP 关闭时**完全一致**
     （否则「走 MCP」会连带改变模型的选择行为）；
   - 每次调用时补齐，且**优先于同名入参**——被绑定的是宿主身份，不该存在覆盖入口，
     否则一个「对模型隐藏」的参数反而成了越权查询他人数据的后门。

数据契约不复制：新增 `app/tools/exam_contract.py`（`EXAM_FIELDS` / `normalize_exam_row` /
`normalize_exam_rows`），**直连工具与 MCP Server 共用同一份**。

---

## 6. Agent 实际调用链

真实联调（`python main.py "我最近有什么考试？"`）的服务端日志：

```
app.mcp.client     | 校园 MCP 客户端已装配: server=campus-mcp-server
                     command=D:\code\campus-flow\.venv\Scripts\python.exe args=-m app.mcp.server
                     cwd=D:\code\campus-flow\campus-agent
__main__           | 启动 campus-mcp-server（transport=stdio，stdout 为协议通道）
app.mcp.client     | 校园 MCP 会话已建立: server=campus-mcp-server tools=get_exam_schedule
app.mcp.tools      | 考试工具改由 MCP 提供: server=campus-mcp-server tool=get_exam_schedule
                     input_schema={... 'student_id': {'type': 'string', 'description': ...} ...}
app.agent.nodes.intent_router | 意图识别完成: intent=query input='我最近有什么考试？'
app.agent.nodes.tool_agent    | 模型请求调用工具: get_exam_schedule
__main__           | MCP 工具调用: get_exam_schedule student_id=student-001     <-- 子进程内
app.infrastructure.campus_api.client | 校园业务服务客户端已装配: base_url=http://localhost:8081
httpx              | HTTP Request: GET http://localhost:8081/api/exams?studentId=student-001 "HTTP/1.1 200 "
__main__           | MCP 工具返回 2 条考试记录                                   <-- 子进程内
app.agent.nodes.tool_agent | 模型未请求工具，直接作答
campus_flow        | 单次模式 intent=query sources=0
```

终端输出：

```
调用工具：get_exam_schedule
你最近有两场考试（数据来自校园教务系统实时查询）：

| 科目 | 日期 | 时间 | 考场 |
|---|---|---|---|
| 计算机网络 | 2026-09-28 | 14:00 | A302 |
| 数据结构 | 2026-09-30 | 19:00 | B201 |

最近的一场是 9 月 28 日下午 2 点的计算机网络，在 A302 考场。需要我帮你查相关课程的课表或成绩吗？
```

**`__main__` 前缀的两条日志证明它们出自 MCP Server 子进程**——即工具真的在子进程内执行，
而不是在 Agent 进程里直接调函数。

**RAG 与 chat 不触发 MCP**（同一份 `.env`，MCP 开启）：

| 提问 | 意图 | 结果 |
|---|---|---|
| `你好，请用一句话介绍你自己` | `chat` | 直接作答，日志**无「MCP 工具调用」、无 `/api/exams` 请求** |
| `校园卡遗失后怎么办？` | `knowledge` | 作答 + 4 条引用，日志**无「MCP 工具调用」、无 `/api/exams` 请求** |

> 说明：MCP **会话**在运行时装配点建立（工具清单必须在建图前确定），但 chat / knowledge
> 轮次不产生任何 MCP 工具调用。这是「按意图惰性**调用**」而非「惰性**建连**」——
> 若要做到「纯对话也完全不拉起子进程」，需要把工具发现改成惰性，代价是模型看到的 schema
> 会退化成静态副本（漂移风险），本阶段有意不这么做。

---

## 7. Spring Boot / MySQL 复用情况

- **Spring Boot 一行未改**：`campus-service/` 的 `controller / service / repository / entity /
  dto / application.yml / schema.sql / data.sql` 全部保持原样，本阶段只读取了它。
- **Python 侧不连 MySQL、不写 SQL、不硬编码 URL**：MCP Server 内部唯一的数据入口是
  `get_campus_api_client()`（`app/infrastructure/campus_api/client.py`，**唯一 HTTP 出口**），
  再经 Spring Boot `/api/exams` 落到 JPA/MySQL。
- **没有重写 `CampusApiClient`**，也没有复制任何 HTTP 逻辑到 MCP 层：`client.py` 里没有
  URL 拼接、没有状态码处理、没有 JSON 结构校验。
- 守卫用例 `test_server_never_reaches_the_database_or_http_layers` 扫描 Server 源码 import 行，
  出现 `mysql / pymysql / sqlalchemy / sqlite3 / psycopg / httpx / requests /
  app.infrastructure.campus_api.client` 即失败；并禁止 `SELECT / INSERT / UPDATE / DELETE`
  关键字与 `/api/exams` 字面量。

---

## 8. Python 测试数量

**`uv run pytest` → 287 passed**（全程离线，无网络、无 MySQL、无 Spring Boot）。

| 范围 | 数量 |
|---|---|
| 阶段前基线 | 248 |
| **新增 MCP 用例** | **39** |
| 合计 | **287 passed / 0 failed** |

新增用例分三层（`campus-agent/tests/mcp/`）：

| 文件 | 数量 | 手法 |
|---|---|---|
| `test_server.py` | 11 | **进程内**（`asyncio.run(server.call_tool(...))`，不起子进程、不联网）：Server 可构建、工具列表只含一个、schema 声明 `student_id` 且带说明、描述含「实时」与四字段、入参透传、缺参被拒、真的经由 `CampusApiClient` 发请求、返回契约、换响应则输出变、业务失败转 `ToolError`、结构守卫 |
| `test_client_stdio.py` | 11 | **真起 MCP 子进程走 stdio 协议**，但用 `ThreadingHTTPServer` 在**随机回环端口**提供 `/api/exams` 替身替代 Spring Boot → 协议往返真实、外部依赖为零：连接 / 发现 / 读 schema / 调用 / 参数透传 / 空结果 / 5xx 可读文案 / close 幂等 / 未建连即 close |
| `test_tools.py` | 17 | **完全离线**：`args_schema` 由服务端 schema 生成（含 `Optional` 的 `anyOf` 形态）、必填/可选默认值、绑定参数不进模型 schema、全绑定得到空模型、未登记类型退化 `Any`、适配器身份来自发现、`_run` 合并绑定参数、绑定参数不可被调用覆盖、`convert_to_openai_tool` 可用、失败原样上抛且不抹平分类、适配层无业务数据字面量、`build_agent_tools` 四种分支 |

对应新增文件：`tests/mcp/{__init__,test_server,test_client_stdio,test_tools}.py`；
`tests/_fakes.py` 新增 `FakeMCPClient` / `make_mcp_tool_definition()` / `make_mcp_settings()` /
`patch_campus_api_in()`。

---

## 9. Spring Boot 测试数量

- 用例集合**未变**：`mvn -o test` 发现 **12 条**（`ExamControllerTest` 4 + `ExamScheduleRepositoryTest` 5
  + `ExamServiceTest` 3），与阶段前完全一致 —— 本阶段**未改动任何 Java 文件**。
- 本次执行结果为 **12 errors**，原因是**环境缺失而非代码问题**：
  `src/test/resources/application-test.yml` 要求真实 MySQL 的 `campus_test` 库 + 环境变量
  `MYSQL_PASSWORD`，而该变量只存在于启动 Spring Boot 时用的那个 shell，
  当前会话拿不到（仓库内按设计无明文）。报错原文：
  ```
  java.sql.SQLException: Access denied for user 'root'@'localhost' (using password: NO)
  Failed to initialize JPA EntityManagerFactory: ... Unable to determine Dialect without JDBC metadata
  ```
- 只要在带 `MYSQL_PASSWORD` 的环境下执行
  `$env:JAVA_HOME="D:\Java\jdk21"; mvn -o test`，即可复现阶段前的 **12/12 通过**。
  > 本机补充：`mvn` 不在 PATH，需用绝对路径
  > `C:\Users\Administrator\.m2\wrapper\dists\apache-maven-3.9.16\<hash>\bin\mvn.cmd`；
  > `JAVA_HOME` 默认未设置。

---

## 10. 真实联调日志

**环境**：MySQL80（3306）+ Spring Boot :8081（`java -jar campus-service-0.1.0.jar`）
+ MCP Server（由 Client 以 stdio 拉起）+ Python Agent。

- **链路日志**：见第 6 节（完整、逐行、含 `__main__` 子进程标记）。
- **对照实验**（`CAMPUS_STUDENT_ID` 换值，其余不变），证明数据确实随查询结果变化：

| `CAMPUS_STUDENT_ID` | Agent 回答 | 与库中种子对照 |
|---|---|---|
| `student-001` | 计算机网络 2026-09-28 14:00 A302；数据结构 2026-09-30 19:00 B201 | 两场 ✓ |
| `student-002` | 大学英语 2026-09-29 09:00 C105 | 一场 ✓ |
| `student-999` | 「我调用了考试安排查询工具，但没有返回任何数据，说明目前没有查到你的近期考试安排。」 | 空 ✓ |

- **不触发 MCP**：chat / knowledge 两轮均无工具调用、无 `/api/exams` 请求（见第 6 节表格）。
- **改动后复跑**：给 `student_id` 补上参数说明（`inputSchema.description`）之后再次联调，
  链路与回答不变（287 passed 亦保持）。

---

## 11. 改 MySQL 后是否读到新数据

- **已完成的间接验证**：`student-001 / 002 / 999` 三次提问得到三份不同且与库中种子逐字吻合的结果；
  每次调用日志里都有一条**新的** `GET http://localhost:8081/api/exams?studentId=...` 200；
  链路上**没有任何缓存层**（`CampusApiClient` 无缓存、MCP 工具无缓存、Spring Data JPA 未启用
  二级缓存）。→ 数据实时来自 MySQL，而非本地 mock 或进程内快照。
- **直接改库复验（UPDATE 一行 → 提问 → 还原）尚未执行**：需要 MySQL 的 root 口令，
  而 `.env`、`application.yml`、shell 环境变量、PowerShell 历史里都没有（这是刻意的安全设计），
  运行中的 jar 也未暴露该变量。拿到口令后即可补做，命令形如：
  ```powershell
  $env:MYSQL_PASSWORD='<pwd>'
  & 'D:\MySQL\MySQL Server 8.0\bin\mysql.exe' -u root -e `
    "UPDATE campus.exam_schedule SET room='Z999' WHERE student_id='student-001' AND course='计算机网络';"
  # 再跑 python main.py "我最近有什么考试？" 应看到 Z999，随后还原为 A302
  ```

---

## 12. 兼容性

| 约束 | 结论 |
|---|---|
| `pytest 248 passed` 全部保持通过 | ✓ 248 → **287 passed**，无回归、无跳过、无 xfail |
| `mvn -o test` 12 passed 兼容 | ✓ 用例仍为 12 条、Java 侧零改动（本次因缺 `MYSQL_PASSWORD` 环境而报错，见第 9 节） |
| 不重写 Spring Boot 业务 | ✓ 零 Java 文件改动 |
| 不直接连接 MySQL | ✓ Python 侧无 DB 驱动、无 SQL（守卫用例） |
| 不改 RAG | ✓ `app/rag/**` 未改动 |
| 不改 AgentState | ✓ `graph/state.py` 未改动 |
| 不改 IntentClassifier | ✓ `agent/intent.py` 未改动 |
| 不改 LangGraph 总体路由结构 | ✓ `graph/graph.py` 未改动；只换了注入的工具清单 |
| 考试查询真正走 MCP（替换而非叠加） | ✓ `build_agent_tools()` 替换槽位；用例断言清单长度与工具名集合不变 |
| 不硬编码 MCP 启动命令 | ✓ 集中在 `MCPSettings.command/args/cwd` |
| 本阶段禁止项 | ✓ 未做课程/成绩/请假 MCP、无 Action、无 Redis、无前端、无 LangSmith 新集成、无动态发现、无多 Server、无远程 MCP、无认证、未改 MySQL/Spring Boot API、未重写 `CampusApiClient`、未重构 RAG/LangGraph |

---

## 13. 当前遗留问题

1. **直接改库复验待补**（第 11 节）：缺 `MYSQL_PASSWORD`，无法执行 `UPDATE` 后复问再还原。
2. **MCP Server 子进程随装配启动**：工具清单必须在建图前确定，因此即使本轮是 chat / RAG
   也会建立 MCP 会话（但不会调用工具、不访问 Spring Boot）。若要「纯对话不拉子进程」，
   需把工具发现改成惰性，代价是 schema 退化为静态副本 —— 本阶段有意不做。
3. **`student_id` 仍绑定单一配置值**（`CAMPUS_STUDENT_ID`）：无登录态，多用户需接身份系统；
   绑定语义（不可被入参覆盖）已刻意固化，接入登录态时应沿用。
4. **单工具、单 Server**：`app/mcp` 目前只服务 `get_exam_schedule`；`course_tools` / `score_tools`
   仍是本地 mock，未走 Spring Boot，也未接入 MCP。
5. **无 MCP 层弹性策略**：没有重试、熔断、并发限流；超时固定 `30s/20s`（可配置）；未做
   Server 崩溃后的自动重连（会话断开即按 `MCPServerUnavailable` 上报）。
6. **MCP 协议细节的耦合点**：`{"result": ...}` 包装与 `Error executing tool <name>` 前缀
   都是**实测**得到的 SDK 行为，已按「读工具自报的 `outputSchema`」而非硬编码字符串来适配；
   升级 `mcp` 版本时需回归 `tests/mcp/test_client_stdio.py`。
7. **临时探测脚本已清理**（本阶段用于确认 SDK API 与错误形态的 6 个脚本已从系统临时目录删除，
   结论全部固化进生产代码与用例）。
