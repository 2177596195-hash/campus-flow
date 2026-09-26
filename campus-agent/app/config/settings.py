"""应用配置中心。

设计约定：
1. 所有敏感信息（API Key、密码等）一律通过环境变量注入，代码内不得硬编码。
2. 配置在首次 ``get_settings()`` / ``get_rag_settings()`` 调用时读取并校验，
   导入模块不产生副作用，便于单元测试与导入期安全性。
3. 新增模型或 Embedding 供应商只需在对应登记表登记，调用方无需改动。
4. 聊天模型配置、RAG 配置与校园业务服务配置**分开获取**：RAG 与业务查询都是
   可选基础设施，缺少对应密钥 / 地址不应影响纯对话链路可用。
5. LangSmith 追踪配置同样单独获取（``get_langsmith_settings()``）：它只是
   **可观测性**，未配置或配置非法时一律降级为「关闭」，绝不阻断启动。
   启用方式只有一处——``apply_langsmith_tracing()`` 把配置写进进程环境，
   剩下的交给 LangChain / LangGraph 官方机制，业务代码里没有任何 tracing 调用。

用法::

    from app.config.settings import get_campus_api_settings, get_rag_settings, get_settings

    settings = get_settings()
    print(settings.llm.model)

    rag = get_rag_settings()
    print(rag.collection_name, rag.chunk_size)

    campus = get_campus_api_settings()
    print(campus.base_url, campus.timeout)

    mcp = get_mcp_settings()
    print(mcp.enabled, mcp.command, mcp.args)

追踪配置（进程启动时调用一次即可，图与节点无需改动）::

    from app.config.settings import apply_langsmith_tracing

    print(apply_langsmith_tracing().enabled)  # 是否真的开启了上报
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 未显式指定 LLM_PROVIDER 时使用的默认供应商
_DEFAULT_LLM_PROVIDER: Final[str] = "deepseek"

# 应用根目录（即 campus-agent/）：本文件位于 <root>/app/config/settings.py
_APP_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

# 模型供应商登记表：环境变量名 -> 默认接入参数
_LLM_PROVIDER_PRESETS: Final[dict[str, dict[str, str]]] = {
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "default_base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
}

#: 未显式指定 EMBEDDING_PROVIDER 时使用的默认供应商
_DEFAULT_EMBEDDING_PROVIDER: Final[str] = "qwen"

# Embedding 供应商登记表。全部走 OpenAI 兼容协议，因此共用同一实现。
# ``supports_token_array_input`` 描述该服务是否接受 token id 数组作为输入：
# DashScope 兼容模式只接受字符串数组（传 token 数组会返回 400），故为 False。
_EMBEDDING_PROVIDER_PRESETS: Final[dict[str, dict[str, object]]] = {
    "qwen": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "text-embedding-v4",
        "supports_token_array_input": False,
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "text-embedding-3-small",
        "supports_token_array_input": True,
    },
}

#: 知识库默认目录（相对应用根目录）
_DEFAULT_KNOWLEDGE_DIR: Final[Path] = _APP_ROOT / "data" / "knowledge"
#: 向量库默认持久化目录（相对应用根目录）
_DEFAULT_PERSIST_DIR: Final[Path] = _APP_ROOT / ".storage" / "vectorstore"
#: 会话检查点 SQLite 数据库默认位置（相对应用根目录）。
#: 与向量库同处 ``.storage/``：都属于「由程序生成、可重建」的本地数据，
#: 因此统一不进版本库（.gitignore 已忽略 ``.storage/``），
#: 也**不放项目根目录**，避免把运行期产物混进源码目录。
_DEFAULT_CHECKPOINT_DB: Final[Path] = (
    _APP_ROOT / ".storage" / "checkpoints" / "campus_flow.sqlite"
)

#: 校园业务服务（Spring Boot）默认地址。
#: 8080 只是约定默认值：端口用环境变量覆盖即可，代码里不存在第二处地址。
_DEFAULT_CAMPUS_SERVICE_BASE_URL: Final[str] = "http://localhost:8080"

#: 尚未接入统一身份认证，当前阶段用固定测试学生代表「当前登录用户」。
_DEFAULT_CAMPUS_STUDENT_ID: Final[str] = "student-001"

#: MCP Server 默认名称（客户端 ``initialize`` 时可见）
_DEFAULT_MCP_SERVER_NAME: Final[str] = "campus-mcp-server"

#: MCP Server 默认启动参数：以模块方式运行 stdio Server
_DEFAULT_MCP_SERVER_ARGS: Final[tuple[str, ...]] = ("-m", "app.mcp.server")

#: MCP Server 启动超时默认值（秒）：要拉起子进程并完成协议握手
_DEFAULT_MCP_STARTUP_TIMEOUT: Final[float] = 30.0

#: MCP 工具调用超时默认值（秒）
_DEFAULT_MCP_CALL_TIMEOUT: Final[float] = 20.0

# ---------------------------------------------------------------------------
# 校园教务日历（当前学期 / 当前教学周）
#
# 这是「今天是什么学期、第几周」在全项目的**唯一生产来源**：工具描述、
# 槽位解析都从这里读，任何模块不得再写死第二份（否则换学期要改多处，
# 漏一处就是喂给模型的过期事实）。尚未接入教务系统校历接口之前，
# 取值由环境变量维护（``CAMPUS_CURRENT_SEMESTER`` / ``CAMPUS_CURRENT_WEEK``）。
# ---------------------------------------------------------------------------

#: 默认当前学期：2026-2027 学年第 1 学期（2026 年 9 月起）
_DEFAULT_CURRENT_SEMESTER: Final[str] = "2026-2027-1"

#: 默认当前教学周
_DEFAULT_CURRENT_WEEK: Final[int] = 3

#: 学期串的形状：``学年起始年-结束年-学期序号``。
#: 只做「能不能当一个学期」的判定；口语解析（本学期 / 上学期）在
#: ``app.agent.slots`` 里，两层各自校验、互不依赖。
_SEMESTER_SHAPE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(\d{4})-([12])$")

# ---------------------------------------------------------------------------
# LangSmith 追踪（可观测性，可选）
#
# 取值口径与**官方 SDK 完全一致**：同样接受规范名与旧名（SDK 的
# ``langsmith.utils.get_env_var`` 命名空间顺序是 LANGSMITH_ 优先于 LANGCHAIN_）。
# 读旧名不是为了多一套配置，而是为了让「我们记录的开关状态」与
# 「SDK 实际会不会上报」不会各说一套。
# ---------------------------------------------------------------------------

#: 规范环境变量名（写进进程环境时只用这几个）
_LANGSMITH_TRACING_ENV: Final[str] = "LANGSMITH_TRACING"
_LANGSMITH_API_KEY_ENV: Final[str] = "LANGSMITH_API_KEY"
_LANGSMITH_PROJECT_ENV: Final[str] = "LANGSMITH_PROJECT"
_LANGSMITH_ENDPOINT_ENV: Final[str] = "LANGSMITH_ENDPOINT"

#: 各配置项的读取顺序（规范名在前，旧名兜底）
_LANGSMITH_TRACING_NAMES: Final[tuple[str, ...]] = (
    _LANGSMITH_TRACING_ENV,
    "LANGCHAIN_TRACING_V2",
)
_LANGSMITH_API_KEY_NAMES: Final[tuple[str, ...]] = (
    _LANGSMITH_API_KEY_ENV,
    "LANGCHAIN_API_KEY",
)
_LANGSMITH_PROJECT_NAMES: Final[tuple[str, ...]] = (
    _LANGSMITH_PROJECT_ENV,
    "LANGCHAIN_PROJECT",
    "LANGCHAIN_SESSION",
)
_LANGSMITH_ENDPOINT_NAMES: Final[tuple[str, ...]] = (
    _LANGSMITH_ENDPOINT_ENV,
    "LANGCHAIN_ENDPOINT",
)

#: SDK 判断「要不要上报」时**优先读**的变量名。它们一旦存在就会盖过
#: ``LANGSMITH_TRACING``，所以配置落地时必须清掉，否则会出现
#: 「配置里关闭了、环境里残留的旧名仍在偷偷上报」。
_LANGSMITH_TRACING_OVERRIDES: Final[tuple[str, ...]] = (
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
)

#: 未配置 ``LANGSMITH_PROJECT`` 时的项目名：与官方 SDK 的默认值一致
#: （不自造一个「更像我们项目」的名字，避免配置与 SDK 行为分叉）。
_DEFAULT_LANGSMITH_PROJECT: Final[str] = "default"

#: 官方 SDK 的默认上报地址。仅用于日志里把「未配置」渲染成可读文本，
#: 真正的地址解析仍在 SDK 内部（不在本项目里复制一份）。
_LANGSMITH_DEFAULT_ENDPOINT: Final[str] = "https://api.smith.langchain.com"


class ConfigurationError(RuntimeError):
    """必填配置缺失或取值非法。"""


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """大模型接入配置。"""

    provider: str
    api_key: str
    base_url: str
    model: str
    temperature: float
    timeout: float
    max_retries: int


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """向量化模型接入配置。"""

    provider: str
    api_key: str
    base_url: str
    model: str
    #: 输出向量维度；为 ``None`` 时使用服务端默认维度。
    dimensions: int | None
    #: 单次请求提交的文本条数上限。
    batch_size: int
    #: 服务端是否接受 token id 数组输入；False 表示只接受字符串数组。
    supports_token_array_input: bool
    timeout: float
    max_retries: int


@dataclass(frozen=True, slots=True)
class RAGSettings:
    """RAG 检索链路配置（加载 / 切分 / 向量库 / 检索）。"""

    knowledge_dir: Path
    persist_dir: Path
    collection_name: str
    vectorstore_backend: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    #: 相关性阈值，取值 ``[0, 1]`` 且越大越相关；低于该值的片段会被丢弃，
    #: ``None`` 表示不过滤。语义由检索层统一，与具体向量库的距离度量无关。
    score_threshold: float | None
    #: 检索时一并交给模型的历史消息条数上限。
    history_window: int
    #: 知识库为空时是否自动摄取；False 时需显式执行 ``--ingest``。
    auto_ingest: bool
    embedding: EmbeddingSettings
    #: 索引清单（增量索引状态）文件路径；为 ``None`` 时落在 ``persist_dir`` 内，
    #: 使「清单」与「向量」同目录、同生命周期。
    manifest_path: Path | None = None


@dataclass(frozen=True, slots=True)
class CampusApiSettings:
    """校园业务服务（Spring Boot）接入配置。

    与 LLM / RAG 配置一样独立取值：业务数据查询属于可选能力，
    服务不可达时只应影响 query / action 分支，不应拖垮对话与知识问答。
    """

    #: 服务基址（不含尾部斜杠），例如 ``http://localhost:8080``
    base_url: str
    #: 单次 HTTP 请求超时（秒）
    timeout: float
    #: 当前学生标识。尚无统一身份认证，先用配置里的固定学生；
    #: 后续接入登录态时替换这一处即可，Tool 与 API Client 无需改动。
    default_student_id: str


@dataclass(frozen=True, slots=True)
class CampusCalendarSettings:
    """校园教务日历事实（当前学期 / 当前教学周）。

    与其他配置一样**惰性获取**（``get_campus_calendar_settings()``）：
    导入模块无副作用，取值非法只影响依赖日历事实的能力，
    不拖垮纯对话与知识问答链路。

    ``current_week`` 当前没有运行期消费者（工具描述不再携带具体周次，
    工具逻辑也用不到它）——保留它是为了让「第几周」这个事实有唯一登记处，
    接入校历接口或前端展示时从这里取。
    """

    #: 当前学期，形如 ``2026-2027-1``（学年起始年-结束年-学期序号）
    current_semester: str
    #: 当前教学周序号
    current_week: int


@dataclass(frozen=True, slots=True)
class MCPSettings:
    """Campus MCP 接入配置。

    与 LLM / RAG / CampusApi 配置各自独立取值，理由一致：MCP 是**可选**的
    调用方式。``enabled=False`` 时 Agent 的考试查询直接调用业务服务
    （少一跳），``True`` 时改走 MCP，两条路径都只经由 ``CampusApiClient``。

    MCP Server 的**启动命令集中在这里管理**，Agent 节点、图中、工具里
    都不出现任何路径或参数，避免换部署方式时要改多处。
    """

    #: 是否让 Agent 经由 MCP 调用校园业务能力
    enabled: bool
    #: MCP Server 名称
    server_name: str
    #: 启动 MCP Server 的可执行文件（默认当前解释器，保证与 Agent 同一虚拟环境）
    command: str
    #: 传给 command 的参数
    args: tuple[str, ...]
    #: 子进程工作目录；``None`` 表示继承当前进程
    cwd: Path | None
    #: 启动（建连 + 握手 + 工具发现）超时（秒）
    startup_timeout: float
    #: 单次工具调用超时（秒）
    call_timeout: float


@dataclass(frozen=True, slots=True)
class AppSettings:
    """应用级配置聚合根。"""

    app_name: str
    environment: str
    log_level: str
    llm: LLMSettings
    #: 会话检查点后端名，取值由 app.agent.graph.checkpoint 校验。
    #: 开发态默认 memory，迁移生产环境时改环境变量即可切换。
    checkpointer_backend: str
    #: SQLite 后端（``CHECKPOINTER_BACKEND=sqlite``）使用的数据库文件路径。
    #: 其余后端忽略该字段——它总是被读出来（与其他配置一致，导入期不做事、
    #: 取值集中在 get_settings），但**只有 sqlite 后端会用到它**。
    #: 默认 ``.storage/checkpoints/campus_flow.sqlite``，可用
    #: ``CHECKPOINTER_SQLITE_PATH`` 覆盖（相对路径按应用根目录解析）。
    checkpointer_sqlite_path: Path


@dataclass(frozen=True, slots=True)
class LangSmithSettings:
    """LangSmith 追踪（可观测性）配置。

    LangSmith 是**可选的观测设施**，与 LLM / RAG / CampusApi / MCP 的配置
    相互独立、单独获取：没配它时项目必须照常运行，配错了也只是不上报。

    字段语义刻意分成两个：

    - ``requested``：环境里的开关本身写了什么（``LANGSMITH_TRACING``）；
    - ``enabled``：**最终是否真的上报**——既看开关，也看有没有 API Key。

    拆开是为了把「没开追踪」与「开了开关但忘配 Key」在日志里区分开，
    排查时不必再去翻 ``.env``。

    ``api_key`` 只留在进程内：``__repr__`` 被改写成只显示「是否已配置」，
    避免任何「顺手打印配置」的场合（异常日志、调试 print）把密钥带出去。
    """

    #: 最终生效值：``requested`` 为真**且**拿到了 API Key
    enabled: bool
    #: 环境里追踪开关的原始取值
    requested: bool
    #: LangSmith API Key；未配置时为空串。**不得出现在任何日志 / 报告中**
    api_key: str
    #: 上报的项目名；未配置时为官方 SDK 的默认项目名
    project: str
    #: 上报地址；未配置时为空串，表示沿用官方 SDK 的默认地址
    endpoint: str

    def __repr__(self) -> str:
        """安全表示：只暴露「是否已配置」，不暴露密钥本身（含任何片段）。"""

        return (
            f"LangSmithSettings(enabled={self.enabled!r}, requested={self.requested!r}, "
            f"api_key={'<已配置>' if self.api_key else '<未配置>'}, "
            f"project={self.project!r}, "
            f"endpoint={self.endpoint or '<官方默认>'!r})"
        )


#: 配置非法或缺 Key 时使用的降级配置（不启用追踪）。
_DISABLED_LANGSMITH_SETTINGS: Final[LangSmithSettings] = LangSmithSettings(
    enabled=False,
    requested=False,
    api_key="",
    project=_DEFAULT_LANGSMITH_PROJECT,
    endpoint="",
)


def _discover_env_file() -> Path | None:
    """自当前文件向上回溯，定位最近的可读 ``.env``。

    不写死项目层级，因此在 ``campus-agent/`` 或仓库根目录启动均可命中。
    """

    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_environment() -> Path | None:
    """将 ``.env`` 载入进程环境（已存在的变量不覆盖）。

    Returns:
        命中的 ``.env`` 路径；未找到时返回 ``None``。
    """

    env_file = _discover_env_file()
    if env_file is None:
        logger.warning("未发现 .env 文件，将仅依赖进程环境变量")
        return None

    load_dotenv(dotenv_path=env_file, override=False)
    logger.debug("已加载环境变量文件: %s", env_file)
    return env_file


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_first(*names: str) -> str:
    """按顺序返回第一个「已配置且非空白」的变量值；都没有时返回空串。

    用于「规范名 + 兼容旧名」的读取。LangSmith 的旧名（``LANGCHAIN_*``）
    由官方 SDK 继续支持，这里一并读，只为让本项目的判定与 SDK 的实际行为一致。
    """

    for name in names:
        value = _env(name)
        if value:
            return value
    return ""


def _parse_bool(name: str, raw: str) -> bool:
    """把布尔文本解析为 ``bool``；无法识别时抛出带变量名的配置错误。"""

    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"环境变量 {name} 必须为布尔值(true/false)，当前值: {raw!r}")


def _read_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 必须为数字，当前值: {raw!r}") from exc


def _read_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 必须为整数，当前值: {raw!r}") from exc


def _read_optional_path(name: str) -> Path | None:
    """读取可选路径配置，未配置时返回 ``None``（由调用方决定默认值）。"""

    raw = _env(name)
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (_APP_ROOT / path)


def _read_optional_int(name: str) -> int | None:
    raw = _env(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 必须为整数，当前值: {raw!r}") from exc


def _read_optional_float(name: str) -> float | None:
    raw = _env(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 必须为数字，当前值: {raw!r}") from exc


def _read_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return _parse_bool(name, raw)


def _read_bool_from(names: tuple[str, ...], default: bool) -> bool:
    """按给定顺序读取第一个已配置的布尔变量（供「规范名 + 旧名」的开关使用）。"""

    for name in names:
        raw = _env(name)
        if raw:
            return _parse_bool(name, raw)
    return default


def _read_path(name: str, default: Path) -> Path:
    """读取路径配置，相对路径按应用根目录解析。"""

    raw = _env(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (_APP_ROOT / path)


def _build_llm_settings() -> LLMSettings:
    provider = _env("LLM_PROVIDER", _DEFAULT_LLM_PROVIDER).lower()
    preset = _LLM_PROVIDER_PRESETS.get(provider)
    if preset is None:
        supported = ", ".join(sorted(_LLM_PROVIDER_PRESETS))
        raise ConfigurationError(
            f"不支持的 LLM_PROVIDER: {provider!r}，可选值: {supported}"
        )

    api_key = _env(preset["api_key_env"])
    if not api_key:
        raise ConfigurationError(
            f"缺少环境变量 {preset['api_key_env']}（供应商: {provider}），请在 .env 中配置"
        )

    return LLMSettings(
        provider=provider,
        api_key=api_key,
        base_url=_env(preset["base_url_env"], preset["default_base_url"]).rstrip("/"),
        model=_env("LLM_MODEL", preset["default_model"]),
        temperature=_read_float("LLM_TEMPERATURE", 0.7),
        timeout=_read_float("LLM_TIMEOUT", 60.0),
        max_retries=_read_int("LLM_MAX_RETRIES", 2),
    )


def _build_embedding_settings() -> EmbeddingSettings:
    provider = _env("EMBEDDING_PROVIDER", _DEFAULT_EMBEDDING_PROVIDER).lower()
    preset = _EMBEDDING_PROVIDER_PRESETS.get(provider)
    if preset is None:
        supported = ", ".join(sorted(_EMBEDDING_PROVIDER_PRESETS))
        raise ConfigurationError(
            f"不支持的 EMBEDDING_PROVIDER: {provider!r}，可选值: {supported}"
        )

    api_key_env = str(preset["api_key_env"])
    api_key = _env(api_key_env)
    if not api_key:
        raise ConfigurationError(
            f"缺少环境变量 {api_key_env}（Embedding 供应商: {provider}），请在 .env 中配置"
        )

    return EmbeddingSettings(
        provider=provider,
        api_key=api_key,
        base_url=_env(
            str(preset["base_url_env"]), str(preset["default_base_url"])
        ).rstrip("/"),
        model=_env("EMBEDDING_MODEL", str(preset["default_model"])),
        dimensions=_read_optional_int("EMBEDDING_DIMENSIONS"),
        batch_size=_read_int("EMBEDDING_BATCH_SIZE", 10),
        supports_token_array_input=bool(preset["supports_token_array_input"]),
        timeout=_read_float("EMBEDDING_TIMEOUT", 30.0),
        max_retries=_read_int("EMBEDDING_MAX_RETRIES", 2),
    )


def _build_rag_settings() -> RAGSettings:
    chunk_size = _read_int("RAG_CHUNK_SIZE", 500)
    chunk_overlap = _read_int("RAG_CHUNK_OVERLAP", 80)

    if chunk_size <= 0:
        raise ConfigurationError(f"RAG_CHUNK_SIZE 必须为正整数，当前值: {chunk_size}")
    if chunk_overlap < 0:
        raise ConfigurationError(f"RAG_CHUNK_OVERLAP 不能为负数，当前值: {chunk_overlap}")
    if chunk_overlap >= chunk_size:
        raise ConfigurationError(
            f"RAG_CHUNK_OVERLAP({chunk_overlap}) 必须小于 RAG_CHUNK_SIZE({chunk_size})"
        )

    top_k = _read_int("RAG_TOP_K", 4)
    if top_k <= 0:
        raise ConfigurationError(f"RAG_TOP_K 必须为正整数，当前值: {top_k}")

    history_window = _read_int("RAG_HISTORY_WINDOW", 6)
    if history_window < 0:
        raise ConfigurationError(f"RAG_HISTORY_WINDOW 不能为负数，当前值: {history_window}")

    # 相关性与距离的换算统一由检索器负责，对外分数语义恒为 [0, 1] 且越大越相关，
    # 因此阈值只需在这个区间内校验，与具体向量库无关。
    score_threshold = _read_optional_float("RAG_SCORE_THRESHOLD")
    if score_threshold is not None and not 0.0 <= score_threshold <= 1.0:
        raise ConfigurationError(
            f"RAG_SCORE_THRESHOLD 必须落在 [0, 1] 内（越大越相关），当前值: {score_threshold}"
        )

    return RAGSettings(
        knowledge_dir=_read_path("RAG_KNOWLEDGE_DIR", _DEFAULT_KNOWLEDGE_DIR),
        persist_dir=_read_path("RAG_PERSIST_DIR", _DEFAULT_PERSIST_DIR),
        collection_name=_env("RAG_COLLECTION", "campus_knowledge"),
        vectorstore_backend=_env("VECTORSTORE_BACKEND", "chroma").lower(),
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        top_k=top_k,
        score_threshold=score_threshold,
        history_window=history_window,
        auto_ingest=_read_bool("RAG_AUTO_INGEST", True),
        embedding=_build_embedding_settings(),
        manifest_path=_read_optional_path("RAG_MANIFEST_PATH"),
    )


def _build_campus_api_settings() -> CampusApiSettings:
    base_url = _env("CAMPUS_SERVICE_BASE_URL", _DEFAULT_CAMPUS_SERVICE_BASE_URL).rstrip("/")
    if not base_url:
        raise ConfigurationError("CAMPUS_SERVICE_BASE_URL 不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigurationError(
            f"CAMPUS_SERVICE_BASE_URL 必须以 http:// 或 https:// 开头，当前值: {base_url!r}"
        )

    timeout = _read_float("CAMPUS_SERVICE_TIMEOUT", 5.0)
    if timeout <= 0:
        raise ConfigurationError(f"CAMPUS_SERVICE_TIMEOUT 必须为正数，当前值: {timeout}")

    student_id = _env("CAMPUS_STUDENT_ID", _DEFAULT_CAMPUS_STUDENT_ID)
    if not student_id:
        raise ConfigurationError("CAMPUS_STUDENT_ID 不能为空")

    return CampusApiSettings(
        base_url=base_url,
        timeout=timeout,
        default_student_id=student_id,
    )


def _build_campus_calendar_settings() -> CampusCalendarSettings:
    """读取教务日历事实，写错配置在这里就报出来。

    学期串做形状与学年连续性校验：否则一个 ``2025-2030-1`` 式的笔误
    会一路流进「本学期」的解析结果，变成一次必然查空的工具调用，
    远不如在配置读取时就大声失败。
    """

    semester = _env("CAMPUS_CURRENT_SEMESTER", _DEFAULT_CURRENT_SEMESTER)
    match = _SEMESTER_SHAPE.match(semester)
    if match is None or int(match.group(2)) != int(match.group(1)) + 1:
        raise ConfigurationError(
            "CAMPUS_CURRENT_SEMESTER 必须形如 2026-2027-1"
            "（学年连续、学期序号为 1 或 2），"
            f"当前值: {semester!r}"
        )

    week = _read_int("CAMPUS_CURRENT_WEEK", _DEFAULT_CURRENT_WEEK)
    if week < 1:
        raise ConfigurationError(f"CAMPUS_CURRENT_WEEK 必须为正整数，当前值: {week}")

    return CampusCalendarSettings(current_semester=semester, current_week=week)


def _build_mcp_settings() -> MCPSettings:
    server_name = _env("CAMPUS_MCP_SERVER_NAME", _DEFAULT_MCP_SERVER_NAME)
    if not server_name:
        raise ConfigurationError("CAMPUS_MCP_SERVER_NAME 不能为空")

    # 默认用**当前解释器**启动 MCP Server：Agent 与 Server 必须同处一个
    # 虚拟环境，否则子进程 import 不到本项目依赖。
    command = _env("CAMPUS_MCP_COMMAND") or sys.executable
    if not command:
        raise ConfigurationError(
            "CAMPUS_MCP_COMMAND 为空，且无法推断当前解释器路径，请显式配置"
        )

    raw_args = _env("CAMPUS_MCP_ARGS")
    args = tuple(raw_args.split()) if raw_args else _DEFAULT_MCP_SERVER_ARGS
    if not args:
        raise ConfigurationError("CAMPUS_MCP_ARGS 不能为空")

    # 子进程以应用根目录为工作目录，因此 ``-m app.mcp.server`` 能找到 app 包。
    cwd = _read_optional_path("CAMPUS_MCP_CWD") or _APP_ROOT

    startup_timeout = _read_float("CAMPUS_MCP_STARTUP_TIMEOUT", _DEFAULT_MCP_STARTUP_TIMEOUT)
    if startup_timeout <= 0:
        raise ConfigurationError(
            f"CAMPUS_MCP_STARTUP_TIMEOUT 必须为正数，当前值: {startup_timeout}"
        )

    call_timeout = _read_float("CAMPUS_MCP_CALL_TIMEOUT", _DEFAULT_MCP_CALL_TIMEOUT)
    if call_timeout <= 0:
        raise ConfigurationError(
            f"CAMPUS_MCP_CALL_TIMEOUT 必须为正数，当前值: {call_timeout}"
        )

    return MCPSettings(
        enabled=_read_bool("CAMPUS_MCP_ENABLED", False),
        server_name=server_name,
        command=command,
        args=args,
        cwd=cwd,
        startup_timeout=startup_timeout,
        call_timeout=call_timeout,
    )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """返回进程内共享的对话侧配置实例。

    首次调用会加载 ``.env`` 并校验必填项。

    Raises:
        ConfigurationError: 必填配置缺失或取值非法。
    """

    load_environment()
    settings = AppSettings(
        app_name=_env("APP_NAME", "CampusFlow"),
        environment=_env("APP_ENV", "development"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        llm=_build_llm_settings(),
        checkpointer_backend=_env("CHECKPOINTER_BACKEND", "memory").lower(),
        checkpointer_sqlite_path=_read_path(
            "CHECKPOINTER_SQLITE_PATH", _DEFAULT_CHECKPOINT_DB
        ),
    )
    logger.debug("配置加载完成: env=%s provider=%s", settings.environment, settings.llm.provider)
    return settings


@lru_cache(maxsize=1)
def get_rag_settings() -> RAGSettings:
    """返回进程内共享的 RAG 配置实例（惰性校验）。

    与 ``get_settings()`` 分离，使缺少 Embedding 密钥时纯对话链路依旧可用。

    Raises:
        ConfigurationError: RAG 相关配置缺失或取值非法。
    """

    load_environment()
    settings = _build_rag_settings()
    logger.debug(
        "RAG 配置加载完成: backend=%s collection=%s embedding=%s",
        settings.vectorstore_backend,
        settings.collection_name,
        settings.embedding.model,
    )
    return settings


def app_root() -> Path:
    """返回应用根目录（``campus-agent/``），供模块解析相对路径。"""

    return _APP_ROOT


@lru_cache(maxsize=1)
def get_campus_api_settings() -> CampusApiSettings:
    """返回进程内共享的校园业务服务配置实例（惰性校验）。

    与 ``get_settings()`` / ``get_rag_settings()`` 分离，理由一致：
    Tool Calling 是可选能力，配置缺失或非法不应影响纯对话与知识问答。

    Raises:
        ConfigurationError: 地址、超时或学生标识配置非法。
    """

    load_environment()
    settings = _build_campus_api_settings()
    logger.debug(
        "校园业务服务配置加载完成: base_url=%s timeout=%s student_id=%s",
        settings.base_url,
        settings.timeout,
        settings.default_student_id,
    )
    return settings


@lru_cache(maxsize=1)
def get_campus_calendar_settings() -> CampusCalendarSettings:
    """返回进程内共享的教务日历配置实例（惰性校验）。

    与 ``get_settings()`` / ``get_campus_api_settings()`` 分离，理由一致：
    日历事实是工具描述与槽位解析的**可选输入**，配置非法不应影响纯对话链路。

    Raises:
        ConfigurationError: 学期串或教学周取值非法。
    """

    load_environment()
    settings = _build_campus_calendar_settings()
    logger.debug(
        "校园教务日历配置加载完成: current_semester=%s current_week=%s",
        settings.current_semester,
        settings.current_week,
    )
    return settings


@lru_cache(maxsize=1)
def get_mcp_settings() -> MCPSettings:
    """返回进程内共享的 Campus MCP 配置实例（惰性校验）。

    MCP 是**可选**调用方式：``CAMPUS_MCP_ENABLED`` 默认 false，此时 Agent
    直连业务服务。开启后 Agent 的考试查询改走 MCP，链路多一跳但工具由
    MCP Server 统一暴露。

    与其它配置入口分开的理由一致：MCP 配置缺失或非法不应影响对话与知识问答。

    Raises:
        ConfigurationError: 启动命令 / 超时 / 名称配置非法。
    """

    load_environment()
    settings = _build_mcp_settings()
    logger.debug(
        "MCP 配置加载完成: enabled=%s server=%s command=%s",
        settings.enabled,
        settings.server_name,
        settings.command,
    )
    return settings


def _build_langsmith_settings() -> LangSmithSettings:
    """读取 LangSmith 追踪配置。

    只读环境变量，不做任何上报动作——真正的启用是
    ``apply_langsmith_tracing()`` 的事，本函数纯读取、可在测试里随意调用。

    Raises:
        ConfigurationError: 开关不是布尔值，或 ENDPOINT 不是 http(s) 地址。
    """

    requested = _read_bool_from(_LANGSMITH_TRACING_NAMES, False)
    api_key = _env_first(*_LANGSMITH_API_KEY_NAMES)
    project = _env_first(*_LANGSMITH_PROJECT_NAMES) or _DEFAULT_LANGSMITH_PROJECT

    endpoint = _env_first(*_LANGSMITH_ENDPOINT_NAMES).rstrip("/")
    if endpoint and not endpoint.startswith(("http://", "https://")):
        # 只校验「是不是个合法地址」，不校验可达性：能不能连上由 SDK 在
        # 后台上报时决定，和校园业务服务地址一样属于配置期能拦住的问题。
        raise ConfigurationError(
            f"环境变量 {_LANGSMITH_ENDPOINT_ENV} 必须以 http:// 或 https:// 开头，"
            f"当前值: {endpoint!r}"
        )

    enabled = requested and bool(api_key)
    if requested and not api_key:
        # 「开了开关却没配密钥」是最常见的误配。这里明确降级为不上报，
        # 而不是让每轮对话都在后台报一次鉴权失败——可观测性不该干扰主流程。
        logger.warning(
            "LangSmith 追踪已开启（%s）但未配置 API Key（%s），"
            "本次运行不启用追踪；补齐密钥后启动即生效",
            _LANGSMITH_TRACING_NAMES[0],
            _LANGSMITH_API_KEY_NAMES[0],
        )

    return LangSmithSettings(
        enabled=enabled,
        requested=requested,
        api_key=api_key,
        project=project,
        endpoint=endpoint,
    )


@lru_cache(maxsize=1)
def get_langsmith_settings() -> LangSmithSettings:
    """返回进程内共享的 LangSmith 追踪配置实例（惰性校验）。

    与其它配置入口分开获取的理由一致——而且这里更彻底：LangSmith 只是观测设施，
    配置缺失或非法都不该影响 Agent 启动。因此这个函数**会**抛
    ``ConfigurationError``（配置错误要能被测出来），但调用方
    ``apply_langsmith_tracing()`` 会把它降级为「关闭」。

    Raises:
        ConfigurationError: 开关或 ENDPOINT 取值非法。
    """

    load_environment()
    settings = _build_langsmith_settings()
    logger.debug(
        "LangSmith 配置加载完成: tracing=%s enabled=%s project=%s endpoint=%s",
        settings.requested,
        settings.enabled,
        settings.project,
        settings.endpoint or _LANGSMITH_DEFAULT_ENDPOINT,
    )
    return settings


def apply_langsmith_tracing(
    settings: LangSmithSettings | None = None,
) -> LangSmithSettings:
    """把 LangSmith 追踪配置落到进程环境——即官方 LangChain/LangGraph 的启用方式。

    为什么是「写环境变量」而不是别的：LangChain / LangGraph 的追踪由
    ``langsmith.utils.tracing_is_enabled()`` 在**每次运行之前**读环境变量决定，
    开启后 ``graph.invoke`` / ``graph.stream`` / 每次模型与工具调用都会自动产生
    trace。因此本函数是**唯一**的接入点，节点、图、RAG、MCP 里没有任何
    tracing 代码，也**不允许**出现（有结构守卫用例）。

    它只做四件事：

    1. 把开关写成 SDK 要求的字面量 ``true`` / ``false``。SDK 是**精确比较**，
       ``True`` / ``yes`` / ``1`` 都不会被识别；配置层接受这些写法，
       所以必须在这里归一化，否则会出现「配置说开了、SDK 认为没开」；
    2. 清掉优先级更高的 ``*_TRACING_V2`` 旧名，否则它们会盖过第 1 步的取值；
    3. 用规范名写回 project / endpoint，使我们日志里的取值就是 SDK 用的取值；
    4. 配置非法或缺 Key 时降级为关闭并告警——追踪出问题不该拖垮主流程。

    **API Key 只写进环境、只读进内存，绝不打印**：日志里只有「已配置 / 未配置」。

    Args:
        settings: 注入的配置；``None`` 时按环境变量解析。

    Returns:
        最终生效的配置（可能与入参不同：被降级过）。
    """

    try:
        if settings is None:
            # 「落地」是启动时的一次性动作，但它读到的必须是**当前**环境：
            # 配置入口带 ``lru_cache``，若在本次调用之前已经被读过（例如先以
            # 未配置状态读过一次），缓存会把旧结论带过来，于是「环境里开了追踪、
            # 落地后却没开」。这里先清掉自己的缓存，让 apply 的语义是
            # 「按现在这一刻的环境算一次并写进去」。
            get_langsmith_settings.cache_clear()
        resolved = settings or get_langsmith_settings()
    except ConfigurationError as exc:
        logger.warning("LangSmith 配置非法，本次运行不启用追踪: %s", exc)
        resolved = _DISABLED_LANGSMITH_SETTINGS

    os.environ[_LANGSMITH_TRACING_ENV] = "true" if resolved.enabled else "false"
    for override in _LANGSMITH_TRACING_OVERRIDES:
        os.environ.pop(override, None)
    if resolved.api_key:
        os.environ[_LANGSMITH_API_KEY_ENV] = resolved.api_key
    if resolved.project:
        os.environ[_LANGSMITH_PROJECT_ENV] = resolved.project
    if resolved.endpoint:
        os.environ[_LANGSMITH_ENDPOINT_ENV] = resolved.endpoint

    _reset_langsmith_env_cache()

    logger.info(
        "LangSmith | tracing=%s project=%s endpoint=%s api_key=%s",
        "enabled" if resolved.enabled else "disabled",
        resolved.project,
        resolved.endpoint or _LANGSMITH_DEFAULT_ENDPOINT,
        "已配置" if resolved.api_key else "未配置",
    )
    return resolved


def _reset_langsmith_env_cache() -> None:
    """让官方 SDK 下次重新读环境变量，避免先前的读取把取值钉死。

    ``langsmith.utils.get_env_var`` 与 ``get_tracer_project`` 都是 ``lru_cache``：
    若在第 3 步写入之前已被调用过，之后写环境变量也不会生效；
    ``run_trees._CLIENT`` 则是全局单例（缓存了 API 地址与后台上报线程）。
    这里不复制 SDK 的任何判断逻辑，只是让它的缓存失效。

    import 放在函数内并容忍缺失：``langsmith`` 是 ``langchain`` 的依赖，
    正常情况下一定装得上；万一没有，说明追踪根本不是可用能力，直接返回即可。
    """

    try:
        from langsmith import run_trees, utils as langsmith_utils
    except ImportError:  # pragma: no cover - 依赖链缺失时无缓存可清
        logger.debug("未安装 langsmith，跳过其环境缓存清理")
        return

    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()
    run_trees._CLIENT = None  # noqa: SLF001 - 仅重置缓存，不读取其内部状态
