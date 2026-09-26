# 课程查询 MCP 化 —— 阶段完成报告

本阶段目标：把 `get_course_schedule` 从**普通 Tool**改为经**现有 Campus MCP Server** 调用，
链路变为：

```
Agent → MCP Client → Campus MCP Server → CampusApiClient → HTTP → Spring Boot → MySQL
                                                                            ↓
Agent ← 自然语言回答 ← ──────────── 返回真实课程数据 ──────────────────────────
```

**本阶段只做了这一件事**：未新建 MCP Server、未把成绩搬上 MCP、未改动 `AgentState` /
`IntentClassifier` / LangGraph 拓扑 / RAG / `score_tools.py` / Spring Boot 成绩与考试代码，
未引入 Redis / LangSmith 之外的组件 / 前端 / 登录 / 请假 / Action / 重试熔断 / 缓存 / 动态工具发现。
Java 侧**零改动**（`git diff` 中 `campus-service/` 无文件变更）。

---

## 1. 修改文件清单

本阶段全部改动都在 Python 侧，共 **8 个文件**（3 个生产代码 + 5 个测试）：

### 生产代码

| 文件 | 改动 |
|---|---|
| `app/tools/course_contract.py` | **抽出 `week` 唯一取值口径**：新增 `MIN_WEEK=1` / `MAX_WEEK=30` / `validate_week()`（拒绝 `bool`、越界抛 `ValueError`、非整数抛 `TypeError`）。直连工具与 MCP Server 共用这一份，避免两条链路的「第 30 周」判定漂移。`__all__` 相应扩充。 |
| `app/tools/course_tools.py` | 删除本地 `_validate_week` / `MIN_WEEK` / `MAX_WEEK` 定义，改为 import `course_contract` 的 `validate_week`；`__all__` 保留再导出。**签名与返回结构未变**（`get_course_schedule(week=None) -> list[dict[str,str]]`，六字段）。 |
| `app/mcp/server.py` | 在**同一个 Server** 上注册第二个工具 `get_course_schedule`：新增常量 `TOOL_GET_COURSE_SCHEDULE` / `COURSE_TOOL_DESCRIPTION` / `WEEK_DESCRIPTION`；工具内部 `validate_week(week)` → `fetch_course_schedule(student_id, week)` → `normalize_course_rows()`；非法参数与 `CampusApiError` 一律转 `ToolError`。`build_server()` 注册两个工具。 |

### 测试

| 文件 | 改动 |
|---|---|
| `tests/_fakes.py` | 新增课程 MCP 工具定义替身：`MCP_COURSE_TOOL_NAME` / `MCP_COURSE_TOOL_DESCRIPTION` / `MCP_COURSE_TOOL_INPUT_SCHEMA`（**真实 `anyOf` 形态**）、`make_mcp_course_tool_definition()`、`make_mcp_server_tool_definitions()`（返回考试+课程两个定义）。 |
| `tests/mcp/test_server.py` | 单工具断言同步为双工具：`EXPECTED_MCP_TOOL_NAMES`、`_tool_named()` 按名取工具；新增「课程工具与考试工具并存」用例；结构守卫增加「源码必须调用 `.fetch_course_schedule(` 且不得出现 `/api/courses`」。 |
| `tests/mcp/test_client_stdio.py` | 本机回环 stub HTTP 同时服务 `/api/exams` 与 `/api/courses`（按 `week` 过滤）；新增课程工具**真实 stdio 协议往返** 7 例（schema 可发现 / 跨进程调用 / week 透传 / week 可选 / 空结果非错误 / 业务错误可读 / week 非法被拒）。 |
| `tests/mcp/test_course_tool.py` | **新建**，35 例，逐项覆盖需求七的 11 条 + 回归。 |

> 说明：`app/mcp/tools.py` 的课程槽位改动（`_resolve_spec`、`_build_mcp_tool`、
> `_MCP_TOOL_BUILDERS`、`build_agent_tools` 按槽位替换）在上一步已完成，
> 本阶段仅修正了一处过时注释（`EXAM_TOOL_NAME` 不再称为「本阶段唯一的工具名」）。

---

## 2. MCP 链路

### 2.1 仍是「一个 Server、一条会话、两个工具」

```
MCP Server（stdio，单进程）
├── get_exam_schedule   (student_id)              保留，行为不变
└── get_course_schedule (student_id, week=None)   本阶段新增
```

两个工具由 `build_server()` 注册在**同一个** `MCPServer` 实例上。适配层
`build_course_mcp_tool()` 与 `build_exam_mcp_tool()` 共用同一个 `StdioMCPClient`，
`start()` 幂等，因此**只拉起一个子进程**。启动日志：

```
校园 MCP 会话已建立: server=campus-mcp-server tools=get_exam_schedule, get_course_schedule
```

### 2.2 工具职责边界

- 课程 MCP 工具**只**调用 `CampusApiClient.fetch_course_schedule(...)`；
  MCP Server 内**没有** MySQL 连接、**没有** SQL、**没有** URL 拼接、**没有** `/api/courses` 字面量
  （由 `test_server.py` 的结构守卫强制）。
- `week` 过滤**在服务端**（Spring Data JPA 按 `week_start/week_end` 范围比较），MCP 侧只透传。
- `week` 的取值范围由 `course_contract.validate_week` 统一判定——直连工具与课程 MCP 工具
  用的是**同一个函数**，所以「第 30 周被接受、第 99 周被拒」在两条链路上完全一致。

### 2.3 参数绑定：`student_id` 绑定、`week` 不绑定

模型实际看到的课程工具 schema（来自 `tools/list`，经适配层剥离绑定参数后）：

```json
{
  "properties": {
    "week": {
      "anyOf": [
        {"description": "教学周序号，例如 3 表示第 3 教学周。当前为第 3 教学周，用户说的「这周 / 本周」即该值；省略时不限教学周，返回整学期课表。",
         "type": "integer"},
        {"type": "null"}
      ],
      "default": null,
      "title": "Week"
    }
  },
  "required": []
}
```

- `student_id` **不在**模型可见 schema 里，调用时由宿主用 `CAMPUS_STUDENT_ID` 补齐，
  且**优先于**同名入参（不存在越权查他人数据的入口）——与考试工具完全一致。
- `week` 属于用户意图，仍由模型按措辞决定填不填。

### 2.4 适配层的一处关键修复：`anyOf` 形态

MCP SDK 为 `week: int | None = None` 生成的 `inputSchema` 是 `anyOf` 形态——
类型与 `description` 都嵌在分支里、顶层没有 `type`。原 `build_args_schema` 只读
`spec.get("type")`，会把它当成 `Any` 并且**丢掉 description**（模型读不到「当前为第 3 教学周」，
只能靠参数名猜）。新增 `_resolve_spec()`：取第一个非 null 分支作为类型来源，
并在外层缺 `description/default/title` 时用分支补上。修复后模型可见 schema 如上。
（该修复由一次性探针 `_schema2.json` 对照真实 `tools/list` 结果验证。）

### 2.5 装配：`AGENT_TOOLS` 槽位替换

`AGENT_TOOLS` 的内容（能力槽位）未变，变的只是每个槽位的**实现来源**：

| 槽位 | 本阶段实现 |
|---|---|
| `get_exam_schedule` | **MCP**（不变） |
| `get_course_schedule` | **MCP**（本阶段由「直连」改为「MCP」） |
| `get_score` | 直连（不动） |

`build_agent_tools()` 现按**槽位**逐个替换（每个能力只有一个槽位，不存在两套同时注册）。
**回退粒度是「按工具」**：MCP 整体拉不起来时两个槽位都回退（等价于改造前的整体回退）；
若只是服务端缺某个工具，则**只有那一个槽位**回退为直连，其余仍走 MCP。回退一律 WARNING 记录。

---

## 3. 测试数量

### 3.1 MCP 套件：83 passed

| 文件 | 用例数 |
|---|---|
| `tests/mcp/test_course_tool.py`（新建） | 35 |
| `tests/mcp/test_client_stdio.py` | 19 |
| `tests/mcp/test_tools.py` | 17 |
| `tests/mcp/test_server.py` | 12 |
| **小计** | **83** |

`test_course_tool.py` 逐项对应需求七：

| 需求项 | 用例 |
|---|---|
| 1  tools/list 能发现两个工具 | `test_server_exposes_exam_and_course_tools` |
| 2  参数 schema 正确 | `test_course_tool_schema_shape` / `test_week_stays_optional_integer_with_description` |
| 3  `student_id` 不暴露 / 不可覆盖 / 自动补齐 | `test_bound_student_id_is_not_exposed_to_the_model` / `test_model_cannot_override_the_bound_student_id` |
| 4  `week` 可选 | `test_course_tool_without_week_omits_the_query` |
| 5/6 正确调用 `CampusApiClient`、参数到 `/api/courses` | `test_course_tool_call_reaches_the_campus_api_client` |
| 7  正常返回六字段契约 | `test_course_tool_returns_contract_rows` |
| 8  异常被 `ToolNode` 处理 | `test_toolnode_turns_course_mcp_failure_into_an_error_message` |
| 9  考试 MCP 不回归 | `test_exam_mcp_behaviour_is_unchanged` |
| 10 成绩仍为直连 Tool | `test_score_is_still_a_direct_tool` / `test_score_is_not_registered_on_the_mcp_server` |
| 11 RAG / chat / routing 不碰 MCP | `test_knowledge_branch_does_not_touch_mcp` 等 |

外加失败路径（参数越界 / 缺参 / 服务端异常）与装配回退（单工具缺失 / 整体不可用 /
`CAMPUS_MCP_ENABLED=false`）用例，以及**结构守卫**：课程工具源码中不得出现
（`数据结构` / `计算机网络` / `数据库原理` / `A302` / `B201`）等业务数据字面量。

### 3.2 全量：458 passed

| 文件 | 用例数 |
|---|---|
| `tests/agent/test_tool_calling.py` | 52 |
| `tests/rag/test_evaluation.py` | 41 |
| `tests/infrastructure/test_campus_api_client.py` | 36 |
| `tests/tools/test_score_tools.py` | 36 |
| `tests/mcp/test_course_tool.py` | 35 |
| `tests/infrastructure/test_campus_api_courses.py` | 34 |
| `tests/infrastructure/test_campus_api_scores.py` | 32 |
| `tests/test_config.py` | 30 |
| `tests/rag/test_retriever.py` | 25 |
| `tests/tools/test_course_tools.py` | 25 |
| `tests/tools/test_exam_tools.py` | 22 |
| `tests/rag/test_incremental_ingest.py` | 21 |
| `tests/mcp/test_client_stdio.py` | 19 |
| `tests/mcp/test_tools.py` | 17 |
| `tests/rag/test_score_threshold.py` | 13 |
| `tests/mcp/test_server.py` | 12 |
| `tests/rag/test_retriever_chroma.py` | 8 |
| **合计** | **458** |

### 3.3 Spring Boot：零改动，未跑

Java 侧本阶段无任何文件变更，既有 63 例用例不受影响，因此未重跑（无需重新打包 jar，
MCP Server 是 Python 子进程）。

---

## 4. 全量测试结果

```
$ .venv\Scripts\python.exe -m pytest campus-agent/tests -q
458 passed in 14.21s
```

- **基线**：上一阶段（成绩查询接入后）为 **414 passed**；
- **本阶段**：**458 passed**（+44，全部来自 MCP 套件 39→83 的增长）；
- **失败 / 错误 / 跳过：0**。

---

## 5. 真实联调结果

环境：MySQL 8.0（`campus` 库）+ Spring Boot `campus-service-0.1.0.jar`（`:8081`，已在运行）
+ Agent（`CAMPUS_MCP_ENABLED=true`，`CAMPUS_STUDENT_ID=student-001`，DeepSeek）。

### 5.1 「我第3周有什么课？」——走 MCP ✓

```
校园 MCP 会话已建立: tools=get_exam_schedule, get_course_schedule
课程工具改由 MCP 提供: tool=get_course_schedule
意图识别完成: intent=query
模型请求调用工具: get_course_schedule
MCP 工具调用: get_course_schedule student_id=student-001 week=3
HTTP Request: GET http://localhost:8081/api/courses?studentId=student-001&week=3 "HTTP/1.1 200 "
MCP 工具返回 7 条课程记录

调用工具：get_course_schedule
你第 3 周的课程安排如下（数据来自校园教务系统实时查询）：
| 星期 | 时间 | 课程 | 教师 | 教室 |
| 周一 | 08:00-09:40 | 数据结构 | 李明 | A302 |
| 周一 | 14:00-15:40 | 计算机网络 | 王芳 | B201 |
| 周二 | 08:00-09:40 | 高等数学 | 张伟 | C105 |
| 周三 | 10:00-11:40 | 大学英语 | 刘洋 | D202 |
| 周三 | 14:00-15:40 | 操作系统 | 陈静 | A401 |
| 周四 | 08:00-09:40 | Java程序设计 | 孙浩 | E101 |
| 周四 | 10:00-11:40 | 数据结构 | 李明 | A302 |
本周共 7 节课，其中数据结构安排了两节（周一、周四）。周五及周末没有课程安排。
```

链路证据：模型调用的是 MCP 工具（`MCP 工具调用` 日志 + 会话发现两个工具），
服务端请求为 `/api/courses?studentId=student-001&week=3`，`week` 由模型从「第 3 周」映射而来。

### 5.2 改 MySQL 前基线：「计算机网络在哪间教室上课？」

```
MCP 工具调用: get_course_schedule student_id=student-001 week=None
HTTP Request: GET http://localhost:8081/api/courses?studentId=student-001 "HTTP/1.1 200 "
MCP 工具返回 8 条课程记录
→ 计算机网络的上课教室是 B201。（教师 王芳，周一 14:00-15:40，1-16 周）
```

### 5.3 改 MySQL：`B201 → Z999`

```sql
UPDATE course_schedule SET room='Z999'
 WHERE student_id='student-001' AND course='计算机网络';
-- 改后：id=2  计算机网络  Z999
```

### 5.4 改库后再问：「计算机网络在哪间教室上课？」

```
MCP 工具调用: get_course_schedule student_id=student-001 week=3
HTTP Request: GET http://localhost:8081/api/courses?studentId=student-001&week=3 "HTTP/1.1 200 "
MCP 工具返回 7 条课程记录
→ 计算机网络的上课地点是 Z999 教室。（教师 王芳，周一 14:00-15:40，1-16 周）
```

**读到的是 Z999** ✓ —— 全链路（Agent → MCP → CampusApiClient → Spring Boot → MySQL）
实时反映数据库变更，Agent 侧无任何本地课程数据缓存。

### 5.5 还原 + 复验

```sql
UPDATE course_schedule SET room='B201'
 WHERE student_id='student-001' AND course='计算机网络';
```

还原后核对全表 10 行与改前**逐字一致**（`计算机网络` 回到 `B201`）；
再问一次 Agent：

```
MCP 工具调用: get_course_schedule student_id=student-001 week=None
→ 计算机网络的上课教室是 B201。
```

**恢复成功确认** ✓

### 5.6 回归：考试 MCP 与成绩直连

| 提问 | 工具 | 链路 | 结果 |
|---|---|---|---|
| 「我最近有什么考试？」 | `get_exam_schedule` | **MCP** → `GET /api/exams?studentId=student-001` 200 → 2 条 | ✓ 计算机网络 2026-09-28 14:00 A302；数据结构 2026-09-30 19:00 B201 |
| 「我数据结构考了多少分？」 | `get_score` | **直连** → `GET /api/scores?studentId=student-001` 200 | ✓ 92 分 / 4.0 学分 / 2025-2026-1（日志中**无** `MCP 工具调用`，证明成绩未走 MCP） |

---

## 6. MySQL 修改后是否实时读到新数据

**是。** 见 5.3–5.5：把 `计算机网络` 的教室由 `B201` 改为 `Z999` 后，
**未重启任何进程**，Agent 下一次提问即读到 `Z999`；还原为 `B201` 后再次提问读到 `B201`。
说明链路上没有任何一级（MCP Server / CampusApiClient / Spring Boot / JPA）缓存了课程数据。

---

## 7. 是否回归

**无回归。**

- Python 全量 `458 passed`（基线 414，新增 44 全部通过，零失败）；
- 考试 MCP、成绩直连两条既有链路在真实环境复测通过（见 5.6）；
- RAG / chat / routing 分支不接触 MCP（由用例与真实提问共同确认）；
- `AgentState` / `IntentClassifier` / LangGraph 拓扑 / `score_tools.py` / Spring Boot 均**零改动**；
- MCP 关闭（`CAMPUS_MCP_ENABLED=false`）时工具清单与改造前**逐项相同**，且不建连。

---

## 8. 遗留问题

1. **`CURRENT_WEEK = 3` 仍是硬编码常量**（`app/tools/course_tools.py`）。
   工具描述里靠它把「这周 / 本周」映射成具体周次；接入校历接口后应改为服务端提供。
   本阶段未处理（不在范围内）。
2. **MCP Server 与直连工具各有一份「工具描述」**——课程工具的描述在
   `app/mcp/server.py` 与 `app/tools/course_tools.py` 各写一遍。两者目前保持
   「同等信息量」以避免改变模型的选择行为，但属于双份维护点；若后续工具描述频繁调整，
   可考虑把描述文本也收敛到 `course_contract`（本阶段未做，避免扩大改动面）。
3. **多进程句柄问题（本机环境，非本阶段引入）**：另一进程执行 `--ingest --full`
   删除并重建 Chroma 集合后，长驻进程已持有的句柄会失效，需重新打开。与课程 MCP 无关。
4. **课程 MCP 的 `week` 上限为 30**：与直连工具同口径。若某学期实际教学周超过 30，
   需同步调整 `course_contract.MAX_WEEK`（一处改、两条链路同时生效，这正是抽出的价值）。
