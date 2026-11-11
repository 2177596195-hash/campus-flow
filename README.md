# CampusFlow（智校园）

一个基于 **LangGraph + MCP** 的高校智能事务 Agent 平台，覆盖 Agent 内核（Python）、校园数据服务（Java）、Web 前端（Vue3）三个子系统。用户用自然语言对话即可完成成绩、课表、考试、请假等校园事务查询，并获得基于校园知识库的 RAG 问答。本项目从零开始逐步迭代开发，每一阶段均配有完成报告与自动化验证。

## 整体架构

```
┌─────────────┐     HTTP / Vite 代理     ┌──────────────────┐    HTTP     ┌────────────────┐
│  campus-web │ ───────────────────────→ │  campus-agent    │ ──────────→ │ campus-service │
│  Vue3 聊天前端│ ←─────────────────────── │  LangGraph Agent │             │ Spring Boot 3  │
└─────────────┘   POST /api/agent/chat   │  FastAPI + CLI   │   MCP/工具   │  JPA + MySQL   │
                                         └──────┬───────────┘             └────────────────┘
                                                │
                                    ┌───────────┼────────────┐
                                    │           │            │
                              Chroma 向量库   DeepSeek LLM   LangSmith
                              （RAG 知识问答）  （对话与工具）  （追踪与评测）
```

## 技术栈

### Agent 内核（campus-agent，Python）

| 组件 | 版本 | 作用 |
|------|------|------|
| LangGraph | 1.2+ | Agent 核心编排：意图路由、工具循环、多轮状态机 |
| LangChain | 1.4+ | 模型调用与消息抽象（langchain-openai / langchain-core） |
| MCP | 2.2+ | Model Context Protocol，把成绩/课程查询封装为标准工具服务 |
| FastAPI + Uvicorn | 0.115+ / 0.34+ | Agent 的 HTTP 入口（`/api/agent/chat`），与 CLI 共享同一运行时 |
| langgraph-checkpoint-sqlite | 3.1+ | 多轮会话检查点落盘，进程重启后同一 session 可续聊 |
| langchain-chroma（Chroma） | 1.1+ | 本地向量库，存储校园知识库切片 |
| DeepSeek（deepseek-chat） | - | 对话与工具调用大模型 |
| 通义千问 text-embedding-v4 | - | RAG 向量化（DashScope 兼容模式） |
| LangSmith | 0.14+ | 全链路追踪 + 数据集同步 + 离线评测（检索阈值、回答相关性） |
| pypdf / python-dotenv / httpx | - | PDF 知识加载、环境配置、HTTP 客户端 |
| uv + pytest | - | 依赖管理与测试框架（60+ 个测试文件） |

### 校园数据服务（campus-service，Java）

| 组件 | 版本 | 作用 |
|------|------|------|
| Spring Boot | 3.2.10 | 服务骨架（Java 21） |
| Spring Data JPA | - | ORM，实体 ↔ 数据表映射 |
| MySQL | 8.0+ | 业务数据库（课表、考试、成绩、请假） |
| HikariCP | - | 连接池（ maximum-pool-size=5） |
| schema.sql / data.sql | - | 幂等建表与初始数据（IF NOT EXISTS + INSERT IGNORE），启动即自动执行 |

### Web 前端（campus-web，TypeScript）

| 组件 | 版本 | 作用 |
|------|------|------|
| Vue | 3.5 | 聊天式主界面（Composition API） |
| Vite | 7 | 构建与开发代理 |
| TypeScript | 5.9 | 全量类型化（vue-tsc 类型检查） |
| Pinia | 4 | 会话与应用状态管理 |
| vue-router / axios | 5.3 / 1.20 | 路由与 HTTP 客户端 |

## 项目结构

采用**三子系统分层架构**，每个子系统职责独立：

```
campus-flow/
├── campus-agent/            # Agent 内核（Python，uv 管理）
│   ├── main.py              #   进程入口：装配运行时，只管参数/日志/驱动，不含业务
│   ├── app/
│   │   ├── agent/           #   LangGraph 核心
│   │   │   ├── graph/       #     图拓扑、State 定义、检查点（memory/sqlite 双后端）
│   │   │   ├── nodes/       #     节点：意图路由/闲聊/知识问答/任务槽位/工具 Agent
│   │   │   ├── intent.py    #     意图分类器
│   │   │   ├── slots.py     #     多轮任务槽位（工程化状态管理）
│   │   │   └── model.py     #     LLM 装配（DeepSeek）
│   │   ├── api/             #   FastAPI 入口（/api/agent/chat、/api/agent/health）
│   │   ├── config/          #   配置加载与校验（非法配置直接拒绝启动）
│   │   ├── infrastructure/  #   campus_api 客户端（调用 campus-service）
│   │   ├── mcp/             #   MCP 客户端/服务端/工具（成绩、课程查询 MCP 化）
│   │   ├── rag/             #   RAG 全链路：加载/切分/向量化/检索/服务/评测
│   │   │   ├── embeddings/  #     通义千问 text-embedding-v4
│   │   │   ├── vectorstore/ #     Chroma 本地持久化
│   │   │   ├── retriever/   #     检索（相关度阈值 0.4）
│   │   │   └── evaluation/  #     LangSmith 数据集同步 + 回答相关性评测
│   │   └── tools/           #   课程/考试/成绩/请假工具 + 契约定义
│   ├── data/
│   │   ├── knowledge/       #   知识库语料（FAQ、校园规章 md、体育制度 pdf）
│   │   └── evaluation/      #   检索评测用例
│   └── tests/               #   pytest：agent / tools / mcp / rag / infrastructure / api
│
├── campus-service/          # 校园数据服务（Java 21 + Spring Boot 3）
│   ├── sql/                 #   初始化脚本
│   └── src/main/java/
│       ├── controller/      #   Course / Exam / LeaveApplication / Score 四个控制器
│       ├── dto/             #   请求/响应对象
│       ├── entity/          #   JPA 实体
│       ├── repository/      #   Spring Data 仓库接口
│       └── service/         #   业务逻辑
│
├── campus-web/              # Web 前端（Vue3 + Vite + TS）
│   ├── scripts/             #   20+ 个端到端契约验证脚本（verify-f2/f3/f4-*.mjs）
│   └── src/
│       ├── views/           #   ChatView 聊天主界面
│       ├── components/      #   chat / sidebar 组件
│       ├── api/             #   agent / courses / exams / leaves / scores 接口层
│       ├── lib/             #   各意图的前端流程编排（scoreFlow / agentScoreFlow 等）
│       ├── stores/          #   Pinia（会话、应用状态）
│       └── constants/       #   各意图的示例话术与引导配置
│
└── docs/                    # 14 份阶段完成报告（MCP、RAG、检查点、前端各阶段）
```

### 为什么分三个子系统？

```
campus-web ──────→ campus-agent ──────→ campus-service ──────→ MySQL
   聊天交互            意图理解/编排/问答        数据读写
```

- **campus-service** 只做纯粹的数据 API，不关心"谁在调用"，只监听本机回环地址，天然安全；
- **campus-agent** 是唯一的"大脑"，对话、路由、工具调用、RAG 全在这层，HTTP 与 CLI 两条入口**共享同一套图拓扑和检查点**，行为完全一致；
- **campus-web** 只负责交互与展示，通过 Vite 代理调用 Agent，未来换 App/小程序只需换这层。

## 数据库设计

campus-service 共 **4 张业务表**（`schema.sql` 幂等建表，启动自动执行）：

| 表名 | 说明 | 关键内容 |
|------|------|----------|
| `course_schedule` | 课程表 | 学期、周次、星期、节次、课程名、教师、教室 |
| `exam_schedule` | 考试安排表 | 课程、日期、时间、考场、座位号 |
| `score` | 成绩表 | 学期、课程名、成绩、学分、绩点 |
| `leave_application` | 请假申请表 | 起止时间、事由、状态 |

数据均以当前学生视角组织（`CAMPUS_STUDENT_ID` 环境变量指定学号），Agent 与前端统一按"我的课表 / 我的成绩"语义查询。

## Agent 图流转

LangGraph 状态机的完整拓扑：

```
START
  → intent_router（意图分类：闲聊/知识问答/课表/考试/成绩/请假）
  → task_slot（多轮任务槽位检查：补全缺失参数、恢复上下文）
  → 条件路由
      ├─ chat          闲聊，直接走 LLM
      ├─ knowledge     知识问答，走 RAG（检索校园知识库）
      ├─ agent         事务查询，进入 ReAct 工具循环（tool_agent ⇄ tools）
      └─ placeholder   意图未实现时的兜底提示
  → END
```

- **工具循环**：`tool_agent` 调用 LLM 决定是否取工具，`tools` 节点执行后回送 `tool_agent`，直到给出最终回答；
- **工具双通道**：事务查询既有本地工具实现，也支持 MCP 服务（`campus-mcp-server`，stdio 传输）——成绩与课程查询已 MCP 化，通过 `CAMPUS_MCP_ENABLED` 开关切换；
- **多轮记忆**：检查点后端可选 `memory`（进程内）或 `sqlite`（落盘），前端每轮传同一个 `threadId` 即可续上完整会话状态。

## 已实现功能

### Agent 能力

| 能力 | 说明 |
|------|------|
| 成绩查询 | 按学期查询成绩，支持 MCP 工具通道 |
| 课程查询 | 按周次/学期查询课表，支持 MCP 工具通道 |
| 考试查询 | 查询考试安排 |
| 请假事务 | 请假意图识别与流程引导 |
| 知识问答 | RAG 检索校园 FAQ、规章制度（md/pdf），相关度低于阈值 0.4 时坦诚说不知道 |
| 闲聊兜底 | 非事务对话直接由 LLM 应答 |
| 多轮续聊 | 槽位补全 + 检查点持久化，"那下周呢"这类追问能接住上下文 |

### Agent HTTP 接口（FastAPI）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/agent/chat` | 一轮对话（传入 message + threadId，返回回复与状态） |
| GET | `/api/agent/health` | 健康检查 |

刻意保持**最小入口**：认证、限流、CORS 属于网关职责，本阶段由 Vite 开发代理承担。

### 校园数据接口（Spring Boot，仅监听 127.0.0.1）

| 前缀 | 说明 |
|------|------|
| `/api/courses` | 课表查询 |
| `/api/exams` | 考试安排查询 |
| `/api/scores` | 成绩查询 |
| `/api/leaves` | 请假申请 |

### CLI 能力（`python main.py`）

| 参数 | 说明 |
|------|------|
| `--session-id` | 指定会话 ID（配合 sqlite 检查点可跨进程续聊） |
| `--ingest` / `--full` | 知识库增量 / 全量向量化入库 |
| `--evaluate-rag` | 本地检索评测（retrieval_cases.json） |
| `--sync-rag-dataset` | 同步评测数据集到 LangSmith |
| `--evaluate-rag-langsmith` / `--evaluate-rag-answer` / `--langsmith-full` | LangSmith 在线评测（检索命中率、回答相关性） |

### 前端契约验证（`campus-web/scripts/`）

20+ 个 `verify-*.mjs` 脚本，覆盖 F2 渲染、F3 四类接口（flow / contract / edge / failure-path 四个维度）、F4 Agent 接入，把"页面能不能跑"变成可重复执行的断言。

## 核心技术实现

### 1. 意图路由 + 槽位管理

意图由 `intent_router` 分类后交给 `task_slot` 做槽位检查——多轮里缺参数就追问，不缺就放行。状态放在 LangGraph State 里而非闭包变量，因此任何节点可读、检查点可存。

### 2. MCP 工具化

成绩、课程查询按 MCP（Model Context Protocol）封装成独立服务，Agent 通过 stdio 客户端调用：

```
Agent（MCP 客户端） ⇄ stdio ⇄ campus-mcp-server（MCP 服务端） ⇄ HTTP ⇄ campus-service
```

好处：工具实现与 Agent 进程解耦，换语言重写数据服务时 Agent 层零改动。

### 3. RAG 检索链路

```
data/knowledge（txt/md/pdf） → 加载/切分（500/80） → 千问向量化 → Chroma 入库（支持增量）
查询 → 向量检索 Top4 → 相关度过滤（阈值 0.4） → 拼装上下文 → LLM 生成回答
```

低于阈值的检索结果直接丢弃，宁可回答"知识库里没有"，不编造。

### 4. 可观测与评测（LangSmith）

- 全链路追踪：每次对话的意图、工具调用、检索片段都上报 LangSmith；
- 数据集同步：`retrieval_cases.json` 一键同步成 LangSmith 数据集；
- 离线+在线评测：检索命中率与回答相关性都有量化分数，调参（如阈值）不再靠感觉。

### 5. 配置即校验

`app/config/settings.py` 对全部环境变量做一次性强校验，取值非法抛 `ConfigurationError` 并以退出码 2 结束——不会带着半截配置继续跑。`.env.example` 只放占位符，真实密钥仅存在于被 gitignore 的 `.env`。

### 6. 测试体系

pytest 分层覆盖：agent（图流转/槽位/检查点持久化）、tools（契约测试）、mcp（客户端/服务端/生命周期）、rag（检索/评测/增量入库）、infrastructure（API 客户端）、api（HTTP 入口）。外部依赖（LLM、数据库）全部用 `_fakes.py` 替身，测试可离线秒跑。

## 快速开始

### 环境要求

| 工具 | 版本要求 |
|------|----------|
| Python | 3.11（uv 管理） |
| Node.js | 22.12+ |
| JDK | 21 |
| Maven | 3.9+ |
| MySQL | 8.0+ |

### 1. 准备配置

```bash
# 在仓库根目录，把模板复制为真实配置
cp .env.example .env
# 编辑 .env，填入以下密钥：
#   DEEPSEEK_API_KEY    DeepSeek 平台申请
#   DASHSCOPE_API_KEY   阿里云百炼平台申请
#   LANGSMITH_API_KEY   LangSmith 平台申请（可选，不开追踪可置 LANGSMITH_TRACING=false）
```

配置文件**不随仓库分发**（含密钥已被 gitignore），需自行创建。

### 2. 启动校园数据服务（campus-service）

```bash
cd campus-service
# MySQL 就绪后，建库：campus（库名由 MYSQL_DATABASE 控制，默认 campus）
mvn spring-boot:run          # 默认 8080；若被占用：SERVER_PORT=8081 mvn spring-boot:run
```

表结构与初始数据由 `schema.sql` / `data.sql` 自动创建，无需手动导入。

### 3. 知识库入库（首次）

```bash
cd campus-agent
uv sync                      # 安装依赖
uv run python main.py --ingest --full   # 全量向量化知识库（之后日常用 --ingest 增量）
```

### 4. 启动 Agent

```bash
# 方式一：CLI 对话（开发调试）
uv run python main.py --session-id demo-01

# 方式二：HTTP 服务（供前端调用）
uv run python -m app.api.server
# 监听 :8000，POST /api/agent/chat
```

注意：Agent 通过 `CAMPUS_SERVICE_BASE_URL` 找 campus-service（默认 `http://localhost:8081`），启动了 8080 就把它改成 8080。

### 5. 启动前端

```bash
cd campus-web
npm install
npm run dev                  # Vite 开发代理已指向 Agent
```

浏览器打开终端输出的地址，输入"查一下我的成绩"即可。

## 项目特点

1. **图编排而非链式拼接**：LangGraph 状态机显式建模意图路由、工具循环与兜底路径，每一步可观测、可回放；
2. **工具协议化**：事务查询按 MCP 标准封装，Agent 与数据服务彻底解耦；
3. **RAG 有度量**：相关度阈值 + LangSmith 离线/在线评测，回答质量有数字兜底；
4. **多轮会话工程化**：槽位管理 + SQLite 检查点，重启进程也能续聊；
5. **契约验证代替手动点检**：前端 20+ 个验证脚本，把回归测试做成一条命令；
6. **测试可离线**：60+ 测试文件全部基于替身实现，不依赖外部服务；
7. **配置安全**：真实密钥永不入库，`.env.example` 只含占位符，配置加载处统一强校验。

## 开发进度

| 模块 | 状态 | 说明 |
|------|------|------|
| Agent 基础结构 | ✅ 已完成 | LangGraph 图拓扑、意图路由、闲聊节点 |
| 成绩查询 | ✅ 已完成 | 工具接入 → MCP 化 |
| 课程查询 | ✅ 已完成 | 工具接入 → MCP 化 |
| 考试查询 | ✅ 已完成 | 工具接入 |
| 请假事务 | ✅ 已完成 | 意图识别 + 流程引导 |
| RAG 知识问答 | ✅ 已完成 | Chroma 入库、增量更新、检索阈值启用 |
| LangSmith 评测 | ✅ 已完成 | 数据集同步、检索命中率、回答相关性 |
| 检查点持久化 | ✅ 已完成 | memory / sqlite 双后端，多轮续聊 |
| 多轮任务状态 | ✅ 已完成 | 槽位工程化，追问上下文可恢复 |
| MCP 阶段 | ✅ 已完成 | stdio 客户端/服务端、生命周期管理 |
| 前端 UI 骨架 | ✅ 已完成 | 聊天主界面、侧边栏、会话管理 |
| 前端真实 API | ✅ 已完成 | 课程/成绩/考试/请假四类接口接入 |
| 前端 Agent 接入 | ✅ 已完成 | 成绩 Agent 链路（threadId 多轮） |

## 许可证

本项目仅供学习交流使用。
