"""Embedding 工厂。

职责边界：只回答「用哪个向量化模型」，不感知向量库、不与向量库耦合。

解耦方式：
1. 供应商信息集中在 ``app.config.settings._EMBEDDING_PROVIDER_PRESETS`` 登记，
   通过 ``EMBEDDING_PROVIDER`` 环境变量切换，代码内不含任何密钥。
2. 对外返回 LangChain 标准的 ``Embeddings`` 抽象，
   因此向量库、检索器都只依赖该接口，未来换成自研模型或本地模型无需改动上游。

注：阿里云 DashScope 兼容模式只接受字符串数组输入，
传 token id 数组会返回 400，故按其预设关闭 ``check_embedding_ctx_length``。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config.settings import EmbeddingSettings, get_rag_settings

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Embedding 构建失败。"""


def build_embeddings(settings: EmbeddingSettings | None = None) -> Embeddings:
    """按配置构建向量化模型。

    Args:
        settings: 显式指定的 Embedding 配置；省略时读取全局 RAG 配置。

    Raises:
        ConfigurationError: 配置缺失或非法。
    """

    embedding_settings = settings or get_rag_settings().embedding
    logger.info(
        "初始化 Embedding: provider=%s model=%s base_url=%s",
        embedding_settings.provider,
        embedding_settings.model,
        embedding_settings.base_url,
    )

    #: ``dimensions`` 仅在显式配置时下发，交由服务端默认值兜底
    optional: dict[str, object] = {}
    if embedding_settings.dimensions is not None:
        optional["dimensions"] = embedding_settings.dimensions

    return OpenAIEmbeddings(
        model=embedding_settings.model,
        api_key=embedding_settings.api_key,
        base_url=embedding_settings.base_url,
        chunk_size=embedding_settings.batch_size,
        request_timeout=embedding_settings.timeout,
        max_retries=embedding_settings.max_retries,
        check_embedding_ctx_length=embedding_settings.supports_token_array_input,
        **optional,  # type: ignore[arg-type]
    )


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """返回进程内共享的向量化模型实例（惰性构建，导入期不读取配置）。"""

    return build_embeddings()


__all__ = ["EmbeddingError", "build_embeddings", "get_embeddings"]
