# 成绩查询 MCP 化 —— 阶段完成报告

本阶段目标：把 `get_score` 从**普通 Tool**改为经**现有 Campus MCP Server**调用，链路变为：

```
Agent → MCP Client → Campus MCP Server → CampusApiClient → HTTP → Spring Boot → MySQL
                                                                        ↓
Agent ← 自然语言回答 ← ─────────── 返回真实成绩数据 ─────────────────────
```

**本阶段只做了这一件事**：未新建 MCP Server、未改动 `AgentState` / `IntentClassifier` /
LangGraph 拓扑 / RAG / 考试与课程两条既有 MCP 链路 / Spring Boot 任何代码，
未引入 Redis / LangSmith 组件 / 前端 / 登录 / 请假 / Action / 缓存 / 重试熔断 / 动态工具发现。
Java 侧**零改动**（见 §6）。

---

## 1. 修改文件清单

共 **9 个文件**（4 个生产代码 + 5 个测试），全部在 Python 侧：

### 生产代码

| 文件 | 改动 |
|---|---|
| `campus-agent/app/tools/score_contract.py` | **抽出两个查询参数的唯一语义**，供直连工具与 MCP Server 共用：新增 `INVISIBLE_CHARS`、`normalize_optional_text(value, *, name)`（非 `str`/`None` 抛 `TypeError`、空串与纯空白视为「未提供」返回 `None`、剔除 BOM/零宽字符）、`match_course(row_course, query)`（双向包含）、`select_by_course(rows, course)`（`course` 为 `None` 原样返回，否则按双向包含筛选）。`__all__` 相应扩充。 |
| `campus-agent/app/tools/score_tools.py` | 删除本地 `_INVISIBLE_CHARS` / `_clean_text` / `_match_course`，改为 import `score_contract`；函数体改为 `normalize_optional_text(...)` → `fetch_scores(semester=...)` → `normalize_score_rows(...)` → `select_by_course(...)`。**工具签名与返回结构未变**（`get_score(semester=None, course=None) -> list[dict[str, str \| float]]`，四字段）。 |
| `campus-agent/app/mcp/server.py` | 在**同一个 Server** 上注册第三个工具 `get_score`：新增常量 `TOOL_GET_SCORE` / `SCORE_TOOL_DESCRIPTION` / `SEMESTER_DESCRIPTION` / `COURSE_NAME_DESCRIPTION`；内部 `normalize_optional_text` 的两处 `TypeError` 转 `ToolError`；`fetch_scores(student_id=..., semester=...)` → `normalize_score_rows` → `select_by_course`；`CampusApiError` 转 `ToolError`。`build_server()` 注册三个工具。 |
| `campus-agent/app/mcp/tools.py` | 新增 `SCORE_TOOL_NAME` / `SCORE_TOOL_BOUND_ARGUMENT` / `build_score_mcp_tool(...)`；`_MCP_TOOL_BUILDERS` 由两项扩为三项 `(get_exam_schedule, get_course_schedule, get_score)`；`__all__` 扩充。适配层本身（`_resolve_spec` / `build_args_schema` / `MCPToolAdapter` / `build_agent_tools`）**逻辑未改**，只是多喂了一个槽位。 |

### 测试

| 文件 | 改动 |
|---|---|
| `campus-agent/tests/_fakes.py` | 新增成绩 MCP 工具定义替身：`MCP_SCORE_TOOL_NAME` / `MCP_SCORE_TOOL_DESCRIPTION` / `MCP_SCORE_TOOL_INPUT_SCHEMA`（**真实 `anyOf(string\|null)` 形态**）、`make_mcp_score_tool_definition()`；`make_mcp_server_tool_definitions()` 改为返回**三个**定义；`FakeMCPClient` 新增 `payloads=`（按工具名分派返回值），原 `payload=` 保留。 |
| `campus-agent/tests/mcp/test_server.py` | `EXPECTED_MCP_TOOL_NAMES` 扩为三工具；新增 `test_all_tools_share_one_server`（断言 `tools/list` 顺序恰为三个）；结构守卫增加「源码必须出现 `.fetch_scores(` 且**不得**出现 `/api/scores` 字面量」。 |
| `campus-agent/tests/mcp/test_client_stdio.py` | 本机回环 stub HTTP 增加 `/api/scores` 服务（按 `semester` query 过滤），并**记录完整 query dict** 供断言「course 不进请求」；新增成绩工具**真实 stdio 协议往返** 8 例；原工具发现用例改为断言三工具。 |
| `campus-agent/tests/mcp/test_course_tool.py` | 同步既有断言到三工具：`test_server_exposes_exam_and_course_tools` → 断言三个；删除 `test_score_is_still_a_direct_tool` / `test_score_is_not_registered_on_the_mcp_server`，替换为 `test_score_slot_is_now_provided_by_mcp` / `test_score_is_registered_on_the_mcp_server`；`test_mcp_course_and_direct_score_work_in_the_same_batch` → `test_course_and_score_mcp_tools_work_in_the_same_batch`（两个 MCP 工具同批次、共用一条会话）。 |
| `campus-agent/tests/mcp/test_score_tool.py` | **新建**，39 例，逐项覆盖需求七的 16 条 + 回归 + 结构守卫。 |

---

## 2. 三个 MCP 工具最终结构

### 2.1 仍是「一个 Server、一条会话、三个工具」

```
MCP Server（campus-mcp-server，stdio，单进程）
├── get_exam_schedule   (student_id)                           既有，行为不变
├── get_course_schedule (student_id, week=None)                 既有，行为不变
└── get_score           (student_id, semester=None, course=None) 本阶段新增
```

三个工具由 `build_server()` 注册在**同一个** `MCPServer` 实例上；`build_exam_mcp_tool()` /
`build_course_mcp_tool()` / `build_score_mcp_tool()` 共用一个 `StdioMCPClient`，
`start()` 幂等，因此**只拉起一个子进程**、只做一次 `initialize` + `tools/list`。
真实启动日志（`_run1.log`）：

```
校园 MCP 会话已建立: server=campus-mcp-server tools=get_exam_schedule, get_course_schedule, get_score
考试工具改由 MCP 提供: tool=get_exam_schedule
课程工具改由 MCP 提供: tool=get_course_schedule
成绩工具改由 MCP 提供: tool=get_score
```

### 2.2 过滤位置：三个工具刻意不同，且与服务端接口能力对齐

| 工具 | 过滤参数 | 过滤发生在哪 | 理由 |
|---|---|---|---|
| `get_exam_schedule` | 无 | — | 接口不支持 |
| `get_course_schedule` | `week` | **服务端**（`week_start <= week <= week_end`，走索引的范围比较） | 需要数据库参与 |
| `get_score` | `semester` | **服务端**（`semester = ?` 精确匹配） | 学期是数据库里的一列，需精确匹配 |
| `get_score` | `course` | **Python 进程内**（`select_by_course` 双向包含） | 接口只按 `studentId` + `semester` 筛选；把课程名模糊匹配下推等于把一套匹配规则复制进 Java，**未为 MCP 在 Java 侧新增 `course` 参数** |

### 2.3 工具职责边界

- 成绩 MCP 工具**只**调用 `CampusApiClient.fetch_scores(...)`；
  MCP Server 内**没有** MySQL 连接、**没有** SQL、**没有** URL 拼接、**没有** `/api/scores` 字面量
  （由 `test_server.py` 与 `test_score_tool.py` 的结构守卫强制）。
- 成绩归一化与参数语义**完全复用** `app/tools/score_contract.py`——
  与直连工具是**同一对函数**，所以「空串算不算传了」「补全成『某课课程』能不能匹配」
  在两条链路上结果一致，不存在漂移。

### 2.4 参数绑定：三个工具的 `student_id` 都绑定，其余参数都不绑定

| 工具 | 绑定（模型不可见、不可覆盖） | 模型可见、可选 |
|---|---|---|
| `get_exam_schedule` | `student_id` | — |
| `get_course_schedule` | `student_id` | `week` |
| `get_score` | `student_id` | `semester`、`course` |

绑定的语义在 `MCPToolAdapter.merged_arguments`：从模型可见 schema 中**移除**，
调用时由宿主用 `CAMPUS_STUDENT_ID` **补齐**，且**优先于**调用方给出的同名入参——
因此「对模型隐藏学号」不会反过来变成越权查他人成绩的后门。

### 2.5 装配：`AGENT_TOOLS` 槽位替换（三个槽位现在都是 MCP）

| 槽位 | 改造前 | 本阶段 |
|---|---|---|
| `get_exam_schedule` | MCP | **MCP**（不变） |
| `get_course_schedule` | MCP | **MCP**（不变） |
| `get_score` | 直连 | **MCP**（本阶段改动） |

`build_agent_tools()` **按槽位**逐个替换，一个能力只有一个槽位，
**不存在同时注册 MCP 与直连 `get_score` 的情况**。回退粒度仍是「按工具」：
MCP 拉不起来时三个槽位一起回退（等价于整体回退直连）；只缺某一个工具时只有那一个槽位回退，
其余仍走 MCP。回退一律 WARNING 记录。

---

## 3. `get_score` 的 MCP 参数 schema

### 3.1 Server 侧 `tools/list` 原始 `inputSchema`（真实抓取）

```json
{
  "type": "object",
  "title": "get_scoreArguments",
  "required": ["student_id"],
  "properties": {
    "student_id": {
      "type": "string",
      "title": "Student Id",
      "description": "学生学号，例如 student-001"
    },
    "semester": {
      "anyOf": [
        {"type": "string", "description": "学期，形如 2025-2026-1。当前为 2025-2026-1 学期，用户说的「这个学期 / 本学期」即该值；省略时不限学期，返回全部成绩。"},
        {"type": "null"}
      ],
      "default": null,
      "title": "Semester"
    },
    "course": {
      "anyOf": [
        {"type": "string", "description": "课程名称，例如「微观经济学」；省略时返回该范围内全部课程。支持模糊匹配（课程名双向包含）。"},
        {"type": "null"}
      ],
      "default": null,
      "title": "Course"
    }
  }
}
```

来源：`_run1.log` 中 `app.mcp.tools` 打印的 `input_schema={...}` 原样输出，非手写。

### 3.2 模型实际看到的 schema（剥离绑定参数后）

`build_args_schema` 去掉 `student_id`，其余照搬（pydantic 重新序列化后的形态）：

```json
{
  "title": "get_score_arguments",
  "type": "object",
  "properties": {
    "semester": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "default": null,
      "description": "学期，形如 2025-2026-1。当前为 2025-2026-1 学期，用户说的「这个学期 / 本学期」即该值；省略时不限学期，返回全部成绩。"
    },
    "course": {
      "anyOf": [{"type": "string"}, {"type": "null"}],
      "default": null,
      "description": "课程名称，例如「微观经济学」；省略时返回该范围内全部课程。支持模糊匹配（课程名双向包含）。"
    }
  }
}
```

要点：

- `semester` / `course` **保留类型（`string`）与 `description`**，未被降级成 `Any`；
- 两者都 `optional`（不在 `required` 里，`default: null`），模型不填即代表「不限学期 / 不限课程」；
- `student_id` **完全不出现在模型可见 schema 里**（`set(properties) == {"semester", "course"}`）。

### 3.3 可选参数为什么必须走 `_resolve_spec`

MCP SDK 为 `semester: str | None = None` 生成的是 **`anyOf` 形态**——类型与 `description`
都嵌在分支里、顶层没有 `type`。适配层若只读 `spec.get("type")`，会把这类参数一律当成 `Any`
并且**丢掉 description**，模型就读不到「当前为 2025-2026-1 学期」这条关键提示
（它正是模型把用户口语「这个学期」落成具体取值的唯一依据）。
上一阶段课程 MCP 化时引入的 `_resolve_spec` 正好覆盖这个形态，本阶段**直接复用、未再改动**，
由 `test_optional_arguments_stay_typed_and_described` 固化。

### 3.4 入参错误的处理

| 情形 | 行为 |
|---|---|
| `semester` / `course` 传入非字符串（如数字、对象） | MCP SDK 按 JSON Schema 校验先拒，返回的 `ToolError` 消息含 `Input should be a valid string`；直调函数体时由 `normalize_optional_text` 抛 `TypeError` → 转 `ToolError` |
| `semester` / `course` 传空串或纯空白 | 视为「未提供」返回 `None`，**不产生**该 query 参数（空串去做课程名包含匹配会误命中全部记录） |
| `semester` / `course` 带 BOM / 零宽字符 | 入口剔除后再匹配（`str.strip()` 不剔 `\ufeff`） |
| 缺 `student_id` | 模型无法触发（该参数不在可见 schema 内，由宿主补齐） |

---

## 4. 测试数量

### 4.1 MCP 套件：130 passed

| 文件 | 用例数 |
|---|---|
| `tests/mcp/test_score_tool.py`（**新建**） | 39 |
| `tests/mcp/test_course_tool.py` | 35 |
| `tests/mcp/test_client_stdio.py` | 27 |
| `tests/mcp/test_tools.py` | 17 |
| `tests/mcp/test_server.py` | 12 |
| **小计** | **130** |

### 4.2 `test_score_tool.py` 与需求七逐项对应

| 需求项 | 用例 |
|---|---|
| 1 `tools/list` 能发现三个工具 | `test_server_exposes_all_three_tools`；`test_all_tools_share_one_server` |
| 2 参数 schema 正确 | `test_score_tool_schema_shape` / `test_optional_arguments_stay_typed_and_described` / `test_score_tool_description_mentions_fields_and_realtime` |
| 3 `student_id` 不暴露 / 不可覆盖 / 自动补齐 | `test_bound_student_id_is_not_exposed_to_the_model` / `test_model_cannot_override_the_bound_student_id` / `test_bound_student_id_is_filled_in_when_the_model_omits_it` |
| 4 `semester` / `course` 可选 | `test_optional_arguments_stay_typed_and_described` / `test_missing_optional_arguments_keep_the_query_clean` |
| 5 `semester` 透传 | `test_semester_reaches_the_campus_api_client` |
| 6 `course` **不进** 成绩接口 query | `test_course_is_not_sent_to_the_service` / `test_campus_api_client_takes_no_course_parameter`（用 `inspect.signature` 固化 `fetch_scores` 没有 `course` 形参） |
| 7 返回四字段契约 | `test_score_tool_returns_contract_rows`（`score` / `credit` 为 `float`） |
| 8 `course` 本地筛选 | `test_course_argument_filters_the_rows` / `test_course_filter_matches_bidirectionally` / `test_course_filter_returns_empty_when_nothing_matches` |
| 9 `semester` 服务端过滤 | `test_semester_filtering_happens_on_the_service` |
| 10 异常被 `ToolNode` 处理 | `test_toolnode_turns_score_mcp_failure_into_an_error_message` / `test_score_business_failure_is_raised_as_tool_error` / `test_http_5xx_is_translated_into_a_readable_tool_error` / `test_data_format_error_is_translated_into_a_readable_tool_error` / `test_missing_required_argument_is_rejected_by_the_schema` |
| 11/12 考试与课程 MCP 不回归 | `test_exam_slot_is_unchanged` / `test_course_slot_is_unchanged` / `test_score_mcp_tool_shares_the_session_with_the_other_tools` |
| 13/14/15 RAG / chat / routing 不回归 | `test_knowledge_branch_does_not_touch_mcp` / `test_chat_branch_does_not_touch_mcp` / `test_intent_routing_is_unchanged` |
| 16 `CAMPUS_MCP_ENABLED=false` 回退直连成绩 | `test_disabled_mcp_falls_back_to_the_direct_score_tool` / `test_direct_score_tool_still_works_when_mcp_is_disabled` |
| 装配回退粒度 | `test_enabled_mcp_replaces_all_three_slots` / `test_score_slot_falls_back_alone_when_only_it_is_missing` / `test_score_slot_missing_is_reported_by_the_builder` / `test_unavailable_server_falls_back_every_slot_to_direct` |
| 结构与可用性 | `test_mcp_server_source_has_no_score_data_literals` / `test_mcp_adapter_source_has_no_score_data_literals` / `test_score_mcp_adapter_delegates_failures_without_swallowing_them` / `test_score_service_end_to_end_success` / `test_score_mcp_tool_is_usable_by_langchain_function_calling` |

真实 stdio 协议往返（`test_client_stdio.py`，8 例）补充覆盖了「跨进程」这一层：
schema 可发现、跨进程调用、`semester` 到达服务端、两个可选参数缺省时请求干净、
`course` 不出现在 query、空结果非错误、业务错误可读、非法参数被拒。

---

## 5. 全量测试结果

```
$ .venv\Scripts\python.exe -m pytest -q
505 passed in 15.00s
```

| 项目 | 数值 |
|---|---|
| 上一阶段（课程 MCP 化后）基线 | **458 passed** |
| 本阶段 | **505 passed** |
| 增量 | **+47**（MCP 套件 83 → 130，+47，与实际改动完全对应） |
| 失败 / 错误 / 跳过 | **0 / 0 / 0** |

分文件（`--collect-only` 统计，合计 505）：

| 文件 | 用例数 |
|---|---|
| `tests/agent/test_tool_calling.py` | 52 |
| `tests/rag/test_evaluation.py` | 41 |
| `tests/mcp/test_score_tool.py` | 39 |
| `tests/tools/test_score_tools.py` | 36 |
| `tests/infrastructure/test_campus_api_client.py` | 36 |
| `tests/mcp/test_course_tool.py` | 35 |
| `tests/infrastructure/test_campus_api_courses.py` | 34 |
| `tests/infrastructure/test_campus_api_scores.py` | 32 |
| `tests/test_config.py` | 30 |
| `tests/mcp/test_client_stdio.py` | 27 |
| `tests/rag/test_retriever.py` | 25 |
| `tests/tools/test_course_tools.py` | 25 |
| `tests/tools/test_exam_tools.py` | 22 |
| `tests/rag/test_incremental_ingest.py` | 21 |
| `tests/mcp/test_tools.py` | 17 |
| `tests/rag/test_score_threshold.py` | 13 |
| `tests/mcp/test_server.py` | 12 |
| `tests/rag/test_retriever_chroma.py` | 8 |
| **合计** | **505** |

> 环境记录：默认临时目录（`%TEMP%\pytest-of-Administrator`）里累积的旧会话目录在
> pytest 收尾清理时，会触发本机的批量删除保护（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），
> 使进程在**已全部通过**之后以非零码退出、汇总行不打印（日志中 505 个点齐全）。
> 改用工作区内的 `--basetemp` 后退出码为 0、汇总行正常。这是运行环境行为，与代码无关。

---

## 6. Spring Boot 测试结果

**零改动、未重跑。**

- 本阶段 `campus-service/`（Java）**无任何文件变更**——`/api/scores` 已支持
  `studentId` + `semester` 筛选，满足 MCP 工具所需，未新增 `course` 参数（课程名匹配留在 Python 侧）。
- MCP Server 是 Python 子进程，不进入 Spring Boot 构建；jar 未重新打包（沿用正在运行的
  `campus-service-0.1.0.jar`）。
- 既有 Spring Boot 用例**不受影响**：上一阶段（成绩查询接入）的实测结果为
  `Tests run: 63, Failures: 0, Errors: 0, Skipped: 0` / `BUILD SUCCESS`
  （见 `docs/成绩查询接入完成报告.md` §测试结果），本阶段未触碰任何 Java 文件，
  该结论继续成立。**未重跑**——本机 `mvn` 不在 PATH（位于 IntelliJ 内置 Maven），
  且 `MYSQL_PASSWORD` 未注入当前 shell；重跑需要先备好这两项。

---

## 7. 真实联调结果

环境：MySQL 8.0（`campus` 库）+ Spring Boot `campus-service-0.1.0.jar`（`:8081`，改造前后均未重启）
+ Python Agent（`CAMPUS_MCP_ENABLED=true`、`CAMPUS_STUDENT_ID=student-001`、DeepSeek `deepseek-chat`）。

四个工具装配日志在每个进程启动时都出现（`_run1.log`）：

```
MCP | enabled=True server=campus-mcp-server
校园 MCP 会话已建立: tools=get_exam_schedule, get_course_schedule, get_score
成绩工具改由 MCP 提供: tool=get_score input_schema={...}
```

### 7.1 「我数据结构考了多少分？」→ 走 MCP ✓

```
意图识别完成: intent=query input='我数据结构考了多少分？'
模型请求调用工具: get_score
MCP 工具调用: get_score student_id=student-001 semester=None course=数据结构
HTTP Request: GET http://localhost:8081/api/scores?studentId=student-001 "HTTP/1.1 200 "
MCP 工具返回 1 条成绩记录

调用工具：get_score
你的《数据结构》成绩是 **92 分**，学分 4 分，学期为 2025-2026-1。
```

`course=数据结构` 由模型从问句提取并进入 MCP 调用，但**请求里没有 course 参数**——
筛选发生在 MCP Server 进程内。

### 7.2 「我这个学期各科成绩怎么样？」→ `semester` 透传到服务端 ✓

```
意图识别完成: intent=query input='我这个学期各科成绩怎么样？'
MCP 工具调用: get_score student_id=student-001 semester=2025-2026-1 course=None
HTTP Request: GET http://localhost:8081/api/scores?studentId=student-001&semester=2025-2026-1 "HTTP/1.1 200 "
MCP 工具返回 3 条成绩记录

→ 操作系统 95 / 3.5；数据结构 92 / 4.0；计算机网络 88 / 3.0
  共 3 门课，合计 10.5 学分，加权平均分约 91.9
```

模型把口语「这个学期」落成 `2025-2026-1`（依据就是 `semester` 的 description），
并以 **query 参数**下推给服务端——服务端只回了这 3 条。

### 7.3 「我上学期的成绩有哪些？」→ `semester` 另一取值 ✓

```
MCP 工具调用: get_score student_id=student-001 semester=2024-2025-2 course=None
HTTP Request: GET http://localhost:8081/api/scores?studentId=student-001&semester=2024-2025-2 "HTTP/1.1 200 "
MCP 工具返回 2 条成绩记录

→ 大学英语 86 / 2.0；高等数学 84 / 5.0
```

### 7.4 「我的计算机网络成绩是多少？」→ `course` 本地筛选 ✓

```
MCP 工具调用: get_score student_id=student-001 semester=None course=计算机网络
HTTP Request: GET http://localhost:8081/api/scores?studentId=student-001 "HTTP/1.1 200 "
MCP 工具返回 1 条成绩记录

→ 你的「计算机网络」成绩是 **88 分**，学分 3.0，学期为 2025-2026-1。
```

**这是「`course` 不进 `/api/scores`」的最直接证据**：MCP 日志里 `course=计算机网络`，
紧随其后的 HTTP 请求却只带 `studentId`，返回的是全部 5 条中的 1 条（本地筛选后）。

### 7.5 回归：考试与课程两条既有 MCP 工具

| 提问 | 工具 | 链路 | 结果 |
|---|---|---|---|
| 「我最近有什么考试？」 | `get_exam_schedule` | MCP → `GET /api/exams?studentId=student-001` 200 → 2 条 | ✓ 计算机网络 2026-09-28 14:00 A302；数据结构 2026-09-30 19:00 B201 |
| 「我第3周有什么课？」 | `get_course_schedule` | MCP → `GET /api/courses?studentId=student-001&week=3` 200 → 7 条 | ✓ 数据结构 / 计算机网络 / 高等数学… 与改造前一致 |

---

## 8. MySQL 修改 → Agent 实时查询 → 恢复验证

全程**未重启任何进程**（MySQL、Spring Boot、MCP Server、Agent 均保持运行）。

### 8.1 改前基线

```sql
SELECT id, semester, course, score, credit FROM score WHERE course='计算机网络';
-- 2  2025-2026-1  计算机网络  88.00  3.0
```

Agent 提问「我的计算机网络成绩是多少？」→ **88 分**（见 7.4，此时 `_run4.log`）。

### 8.2 改库：`88.00 → 89.00`

```sql
UPDATE score SET score=89.00
 WHERE student_id='student-001' AND course='计算机网络';

SELECT id, semester, course, score, credit FROM score WHERE course='计算机网络';
-- id  semester      course      score  credit
-- 2   2025-2026-1   计算机网络   89.00  3.0      ← 已生效
```

### 8.3 改库后立刻再问同一条问题（`_run5.log`）

```
MCP 工具调用: get_score student_id=student-001 semester=None course=计算机网络
HTTP Request: GET http://localhost:8081/api/scores?studentId=student-001 "HTTP/1.1 200 "
MCP 工具返回 1 条成绩记录

→ 你的「计算机网络」成绩是 **89 分**，学分 3.0，学期为 2025-2026-1。
```

**读到 89** ✓ —— 链路（Agent → MCP → CampusApiClient → Spring Boot → JPA → MySQL）
实时反映数据库变更，没有任何一级缓存成绩数据。

### 8.4 还原：`89.00 → 88.00` 并复验（`_run6.log`）

```sql
UPDATE score SET score=88.00
 WHERE student_id='student-001' AND course='计算机网络';
```

还原后核对**整表 7 行与改前逐字一致**：

```
id  student_id   semester      course      score  credit
1   student-001  2025-2026-1   数据结构     92.00  4.0
2   student-001  2025-2026-1   计算机网络   88.00  3.0     ← 已还原
3   student-001  2025-2026-1   操作系统     95.00  3.5
4   student-001  2024-2025-2   高等数学     84.00  5.0
5   student-001  2024-2025-2   大学英语     86.00  2.0
6   student-002  2025-2026-1   大学物理     90.00  4.0
7   student-002  2025-2026-1   线性代数     84.00  3.0
```

Agent 再问一次：

```
MCP 工具调用: get_score student_id=student-001 semester=None course=计算机网络
→ 你的「计算机网络」成绩是 **88 分**（学分 3.0，学期 2025-2026-1）。
```

**恢复成功确认** ✓

---

## 9. 是否回归

**无回归。**

- Python 全量 **505 passed**（基线 458，增量 47 全部通过，零失败零跳过）；
- 考试 MCP、课程 MCP 两条既有链路在真实环境复测通过（§7.5）；
- 成绩**直连**工具在 `CAMPUS_MCP_ENABLED=false` 时行为与改造前逐项相同（有专门用例固化）；
- RAG / chat / routing 分支不接触 MCP（用例 + 真实提问共同确认）；
- `AgentState` / `IntentClassifier` / LangGraph 拓扑 / `exam_tools.py` / `course_tools.py` /
  Spring Boot 全部**零改动**；
- 适配层对可选参数 `anyOf` 的解析**未改**（复用上一阶段成果），考试与课程工具的可见 schema 逐字未变。

---

## 10. 遗留问题

1. **`CURRENT_SEMESTER = "2025-2026-1"` 仍是硬编码常量**（`app/tools/score_tools.py`）。
   工具描述靠它把「这个学期 / 本学期」映射成具体取值；接入校历接口后应改为服务端提供。
   与课程工具的 `CURRENT_WEEK` 是同一类问题。本阶段未处理（不在范围内）。
2. **工具描述有两份**：成绩工具的描述在 `app/mcp/server.py`（`SCORE_TOOL_DESCRIPTION`）与
   `app/tools/score_tools.py`（docstring）各写一遍，目前刻意保持「同等信息量」以避免
   改变模型的选择行为，但属于双份维护点。三个工具都有这个问题；
   若后续描述频繁调整，可考虑把描述文本也收敛到各自的 `*_contract`。
3. **`course` 筛选留在 Python 侧**（刻意）：`fetch_scores` 只支持 `studentId` + `semester`，
   因此「某门课的成绩」是把该生该学期的一小段列表取回后再挑。
   好处是没为 MCP 在 Java 侧新增参数；代价是当某生成绩量极大时会有多余的传输与归一化开销。
   当前数据量下无实际影响，若将来成绩规模上升，可考虑把课程名筛选下推到服务端
   （届时匹配规则要单点化，避免两边各写一套）。
4. **MCP 服务端仍无 `course` 参数**——这是本阶段的**设计约束**而非缺陷：
   由 `test_campus_api_client_takes_no_course_parameter` 用 `inspect.signature` 固化，
   防止日后有人「顺手」加参数导致两条链路口径分叉。
5. **多进程句柄问题（本机环境，非本阶段引入）**：另一进程执行 `--ingest --full` 删除并重建
   Chroma 集合后，长驻进程已持有的句柄会失效，需重新打开。与成绩 MCP 无关。
6. **pytest 收尾清理触发本机批量删除保护**（见 §5 环境记录）：仅影响运行退出码与汇总行显示，
   不影响判定结果；用 `--basetemp` 指向工作区目录即可规避。
