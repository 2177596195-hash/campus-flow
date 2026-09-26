# LangGraph 检查点持久化（SQLite）完成报告

本阶段把会话检查点从「只活在进程内」升级为「落到本地 SQLite 文件」：**进程 A 留下未完成的
多轮任务，退出后由进程 B 用同一会话续上并真正取到数据**。默认后端仍是 `memory`，持久化是
一个可选的、由配置切换的能力。

---

## 1. 环境与版本（先确认，再动手）

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.11.16 | 仓库根 `.venv` |
| langgraph | 1.2.12 | |
| langgraph-checkpoint | 4.2.0 | **本体只含 `base` / `memory` / `serde`，不含 sqlite** |
| langgraph-checkpoint-sqlite | **3.1.1（本阶段新装）** | 官方独立分发的 SQLite 后端 |
| aiosqlite / sqlite-vec | 0.22.1 / 0.1.9 | 随上一行自动装上 |

安装前后都直接查了安装产物，而不是按记忆里的旧文档写代码：

```
langgraph/checkpoint/ → base, memory, serde      （无 sqlite，确认必须另装包）
langgraph.checkpoint.sqlite.SqliteSaver.__init__ → (self, conn: sqlite3.Connection, *, serde=None)
SqliteSaver.from_conn_string(str)                → @classmethod @contextmanager，yield SqliteSaver
SqliteSaver.close                                → None（**该类没有 close**）
SqliteSaver.setup                                → 惰性建表，官方注明 "should not be called directly by the user"
```

结论：**同步 `SqliteSaver` 要自己持有连接，且没有 `close()`**——这一点直接决定了下面的生命周期设计。

---

## 2. 本阶段改动的文件

| 文件 | 性质 | 改动 |
|---|---|---|
| `campus-agent/app/agent/graph/checkpoint_sqlite.py` | **新增** | SQLite 后端的装配细节：父目录自动创建、建连接、关连接 |
| `campus-agent/app/agent/graph/checkpoint.py` | 修改 | 登记表加入 `sqlite`；新增 `CheckpointerOptions` / `close_checkpointer()`；`create_checkpointer` 增加关键字参数 |
| `campus-agent/app/agent/graph/__init__.py`、`app/agent/__init__.py` | 修改 | 导出新符号 |
| `campus-agent/app/config/settings.py` | 修改 | 新增 `_DEFAULT_CHECKPOINT_DB` 常量与 `AppSettings.checkpointer_sqlite_path` |
| `campus-agent/main.py` | 修改 | 装配时传入 sqlite 路径；新增 `_close_runtime()` 并在 `finally` 里释放后端资源 |
| `pyproject.toml` / `uv.lock` | 修改 | 声明 `langgraph-checkpoint-sqlite` |
| `.env.example` | 修改 | 补 `CHECKPOINTER_BACKEND` 两种取值的说明 + `CHECKPOINTER_SQLITE_PATH` 占位行 |
| `campus-agent/tests/agent/test_checkpoint_sqlite.py` | **新增** | 同进程：登记表 / 配置 / 目录 / 生命周期 / 隔离 / 守卫 |
| `campus-agent/tests/agent/test_checkpoint_persistence.py` | **新增** | **真两进程**持久化用例 |
| `campus-agent/tests/agent/_persistence_probe.py` | **新增** | 上面那份用例拉起的「被测进程」 |

未改动：`graph.py`（拓扑与 checkpointer 注入方式原样）、`state.py`、`slots.py`、`task_slot.py`、
`tool_agent.py`、`intent.py`、MCP 三件套与三个工具、Spring Boot、MySQL、RAG 全链路、LangSmith 评估。

---

## 3. SQLite 是怎么接进去的（登记表 + 工厂，不退化成 if/else）

```python
# app/agent/graph/checkpoint.py
_CHECKPOINTER_FACTORIES: Final[dict[str, Callable[[CheckpointerOptions], BaseCheckpointSaver]]] = {
    DEFAULT_BACKEND: _create_memory,     # "memory"
    SQLITE_BACKEND: _create_sqlite,      # "sqlite"
}

create_checkpointer(backend=None, *, sqlite_path=None)   # 只查表，不按后端名分支
close_checkpointer(saver)                                # 「怎么关」的唯一出口
```

- 所有工厂**统一签名** `CheckpointerOptions -> BaseCheckpointSaver`（值对象目前只有 `sqlite_path`），
  因此调用方不需要「按后端名传不同参数」，新增后端也不必改调用方。
- 缺路径时 `create_checkpointer("sqlite")` **直接抛 `CheckpointerError`**，不猜默认位置。
  （CLI 永远拿得到路径：它来自配置默认值。）
- `memory` 仍在，仍是**默认**；`create_checkpointer("memory")` / `create_checkpointer(settings.checkpointer_backend)`
  这类既有调用一行都不用改。

---

## 4. 文件落在哪、目录怎么来的

默认路径 = `campus-agent/.storage/checkpoints/campus_flow.sqlite`：

- 常量定义在 `app/config/settings.py::_DEFAULT_CHECKPOINT_DB`，可用 `CHECKPOINTER_SQLITE_PATH` 覆盖
  （相对路径按 `campus-agent/` 解析，绝对路径原样用）。
- **与向量库同处 `.storage/`**：都属于「程序生成、可重建」的本地数据；`.gitignore` 里 `.storage/` 早已忽略，
  因此**没有**再落一条忽略规则，也没有把文件放到项目根目录。
- 父目录由 `checkpoint_sqlite.build_sqlite_checkpointer()` 自动创建（全新克隆的仓库里
  `.storage/checkpoints/` 并不存在）。实测：运行前目录不存在 → 运行后文件与目录都在。

---

## 5. 生命周期：不留永不关闭的连接

`SqliteSaver` 自己没有 `close()`，官方给的生命周期原语是 `SqliteSaver.from_conn_string()` 这个
上下文管理器（退出时 `closing()` 关连接）。而图一旦编译完成就必须在 `with` 之外长期使用，
所以本阶段把「建」与「关」拆成两个显式动作：

- `checkpoint_sqlite.build_sqlite_checkpointer(path)` —— 建（含目录、含 `check_same_thread=False`，与官方一致）；
- `checkpoint.close_checkpointer(saver)` —— 关（`None` / `InMemorySaver` 是空操作，`SqliteSaver` 关 `saver.conn`；
  用 `isinstance` 判定，而不是「有没有 `conn` 属性」）；
- `main.py` 的 `finally: _close_runtime(runtime)` 调用它，存储器取自 `runtime.checkpointer`
  （`CompiledStateGraph` 持有注入的 checkpointer）。

**为什么 `_build_runtime` 的返回值签名没动**：`tests/rag/test_langsmith_*.py` 会把 `_build_runtime`
monkeypatch 成「单值返回」，改签名会连带炸掉那两条派发用例；而「运行时本身就是资源持有者」也是事实。
mypy/评审视角下这比另存一份引用更干净。

---

## 6. memory 后端兼容性

- 默认值**没有**从 `memory` 改成 `sqlite`（本阶段的目标是「让持久化成为选项」）。
- 真实对照（同一台机器、同一时刻）：

| 场景 | 后端 | 结果 |
|---|---|---|
| 进程 1：`--session-id real-c "帮我查一下成绩"` | sqlite | `intent=query`，写入待办并追问 |
| **进程 2（新进程）**：`--session-id real-c "上学期"` | sqlite | 孤立识别 `unknown` → **待办恢复** → `get_score` → 回答 2024-2025-2 两门课 |
| 进程 3（新进程）：`--session-id real-other "上学期"` | sqlite | `intent=unknown` → 占位节点（**什么都没继承**） |
| 进程 1：`--session-id mem-demo "帮我查一下成绩"` | memory | 正常追问 |
| **进程 2（新进程）**：`--session-id mem-demo "上学期"` | memory | `intent=unknown` → 占位节点（什么都没继承） |
| 跑完 memory 那次后检查 SQLite 文件 | — | mtime **未变**（没有多余写入） |

同一个「换进程续问」的动作，sqlite 能续上、memory 续不上——这正是本阶段的验收点，
同时也说明 memory 路径没有被改坏。

---

## 7. 测试：数量与结果

- 新增 **32** 条，全部通过：
  - `tests/agent/test_checkpoint_sqlite.py`（30 条）：后端登记表（含「每个已登记后端都真的能建出来」）、
    配置读取（默认值 / 大小写宽容 / 相对路径按应用根解析 / 绝对路径原样）、默认落点、
    父目录自动创建、状态真的写进配置的那个文件、关闭连接（含重复关闭、`None`、memory 空操作、
    `main.py` 收尾真的关掉图里的存储器）、同文件换实例恢复、两个文件互不串、跨 thread 隔离、
    `pending_value` 只消费一次且不写回状态、架构守卫（检查点层不反向依赖上层；图定义不认识具体后端）。
  - `tests/agent/test_checkpoint_persistence.py`（2 条）：见下节。
- **全量回归：845 passed**（本阶段前基线 813 + 新增 32），`exit=0`，用时 96.81s。

---

## 8. 真·跨进程测试（不是同进程双跑）

`InMemorySaver` 在同进程里本来就能恢复状态，所以「同进程连续两次 invoke 通过」证明不了任何持久化能力。
因此这份用例用 `subprocess` 拉起 `tests/agent/_persistence_probe.py`：

- 探针把 `os.getpid()` 一起回报，用例断言 `first["pid"] != second["pid"]` —— **自证是两个进程**；
- 两次运行之间不存在任何共享对象，唯一能携带状态的只有那个 SQLite 文件；
- 探针的外部依赖仍是替身（脚本模型 / 脚本意图 / MockTransport 校园业务服务），所以离线可跑，
  但**图与 SQLite 存储器是真的**；
- 断言的是行为差异而不是「有没有写文件」：进程 B 里「上学期」的**孤立识别结果是 `unknown`**，
  最终却 `intent=query` 且真的调用了 `get_score`——只剩「待办被恢复」一种解释。
- 附带观测点 `hint_seen`：进程 B 补槽位那一轮两次模型调用是 `[True, False]`（任务上下文只出现一次），
  紧接着的下一轮是 `[False]`——把「`pending_value` 只消费一次」变成了可断言事实。

---

## 9. 进程 1 → SQLite → 进程 2 的真实结果（真 MCP + Spring Boot + MySQL）

`CHECKPOINTER_BACKEND=sqlite`，`CAMPUS_MCP_ENABLED=true`，Spring Boot `:8081`，MySQL80 在线。

```
[进程 1] python main.py --session-id real-c "帮我查一下成绩"        → exit 0
  checkpointer=sqlite | 会话检查点文件: ...\.storage\checkpoints\campus_flow.sqlite
  intent=query → 本轮请求缺少槽位 semester，写入待办并追问 → 本轮只输出追问

[进程 2] python main.py --session-id real-c "上学期"               → exit 0
  intent=unknown input='上学期'                    ← 孤立识别：这句话本身什么都不是
  槽位补齐: semester=2024-2025-2 -> 继续任务 query   ← 待办从磁盘恢复并解释本轮输入
  注入任务上下文: pending_value=2024-2025-2
  模型请求调用工具: get_score
  MCP 工具调用: get_score student_id=student-001 semester=2024-2025-2 course=None
  HTTP Request: GET http://localhost:8081/api/scores?studentId=student-001&semester=2024-2025-2 "HTTP/1.1 200"
  MCP 工具返回 2 条成绩记录
  → 回答：大学英语 86/2 学分、高等数学 84/5 学分，加权平均 84.57

[进程 3] python main.py --session-id real-other "上学期"           → exit 0
  intent=unknown → 意图 unknown 命中占位节点（未继承任何待办）
```

SQLite 文件事后直接读出来（只读模式）：

```
db size = 77824 bytes
tables  = ['checkpoints', 'writes']
thread_id='real-c'     checkpoints=11 writes=31
thread_id='real-other' checkpoints=5  writes=10
```

同一份历史里两条会话各自成行 → 跨会话隔离在**存储层**也成立。

---

## 10. 是不是真的走了 MCP → Spring Boot → MySQL

是，进程 2 的日志把三段都留下了（见上一节的完整引用）：

1. **MCP**：`MCP 工具调用: get_score ...`，且工具清单是 MCP 会话建立后从 `tools/list` 发现的
   （三个工具都由 MCP 提供，schema 也打印自 MCP 的 `inputSchema`）；
2. **Spring Boot**：`HTTP Request: GET http://localhost:8081/api/scores?...&semester=2024-2025-2 "HTTP/1.1 200"`；
3. **MySQL**：返回的正是种子数据里 `2024-2025-2` 的两行（大学英语 86/2、高等数学 84/5），
   且这学期的过滤是**服务端**做的（`semester = ?`）。

旁证：直连业务服务接口 `GET /api/scores?studentId=student-001&semester=2024-2025-2` 单独探一次，
返回同样两行。

---

## 11. 不同 thread 的隔离（跨进程）

- `tests/agent/test_checkpoint_persistence.py::test_a_different_thread_in_a_new_process_inherits_nothing`：
  同一数据库、另一个 `thread_id`、另一个进程、同一句「上学期」→ `intent=unknown`、零工具调用、零模型调用。
- 真实运行：进程 3 用 `real-other` 得到 `intent=unknown` → 占位节点。
- 存储层：`SELECT DISTINCT thread_id FROM checkpoints` 得到 `{real-c, real-other}`，各自独立成行。

---

## 12. 遗留问题（如实记录）

1. **只有一个文件一个连接**：没有 PostgreSQL / Redis、没有分布式锁、没有 TTL；多进程并发写同一文件
   只依赖 SQLite 自身的 WAL，本项目未做额外仲裁。
2. **会话历史只增不减**：还没有清理策略（`SqliteSaver.delete_thread()` 可用，但未接入任何入口）。
3. **`langgraph-checkpoint-sqlite` 是独立包**：`langgraph-checkpoint` 本体不含 SQLite，换机器/换环境
   时必须重新 `uv sync`（已写进 `pyproject.toml`，不会再漏）。
4. 上一阶段遗留、本阶段**未处理**：`pending_value` 只存值不存槽位名；多域混合请求刻意不追问；
   待办无 TTL / 无轮次上限；补齐那一轮 LangSmith 的 intent tag 仍是孤立识别结果（真实意图看
   `task_slot` 日志）。其中最后一条的「跨进程不保留」已由本阶段解决。
5. 本阶段**未做**：Postgres / Redis / 分布式锁 / TTL / 会话管理 API / 登录 / 前端历史 / 请假 /
   第二个槽位 / 多任务编排 / LangSmith 新指标 / 目录重构；**未改** Spring Boot、MySQL、MCP Server
   与三个工具、Retriever、`RAG_SCORE_THRESHOLD`、RAG Evaluation、LangSmith Evaluator、业务工具清单。
