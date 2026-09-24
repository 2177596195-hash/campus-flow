"""应用配置中心。

设计约定：
1. 所有敏感信息（API Key、密码等）一律通过环境变量注入，代码内不得硬编码。
2. 配置在首次 ``get_settings()`` / ``get_rag_settings()`` 调用时读取并校验，
   导入模块不产生副作用，便于单元测试与导入期安全性。
3. 新增模型或 Embedding 供应商只需在对应登记表登记，调用方无需改动。
4. 聊天模型配置与 RAG 配置**分开获取**：RAG 是可选基础设施，
   缺少 Embedding 密钥不应影响纯对话链路可用。

用法::

    from app.config.settings import get_rag_settings, get_settings

    settings = get_settings()
    print(settings.llm.model)

    rag = get_rag_settings()
    print(rag.collection_name, rag.chunk_size)
"""

from __future__ import annotations

import logging
import os
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
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"环境变量 {name} 必须为布尔值(true/false)，当前值: {raw!r}")


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
