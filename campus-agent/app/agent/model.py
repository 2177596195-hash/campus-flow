"""大模型工厂。

职责边界：只负责「按配置构建聊天模型」这一件事。
不感知 LangGraph、不感知节点拓扑，图定义通过参数注入模型实例，
从而支持测试替身、多模型路由与后续按意图切换模型的扩展。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config.settings import LLMSettings, get_settings

logger = logging.getLogger(__name__)


def build_llm(settings: LLMSettings | None = None) -> BaseChatModel:
    """按配置构建一个聊天模型实例。

    不做缓存，便于按不同参数（温度、模型名）构造多个实例。

    Args:
        settings: 显式指定的模型配置；省略时读取全局配置。

    Raises:
        ConfigurationError: 全局配置缺失或非法。
    """

    llm_settings = settings or get_settings().llm
    logger.info(
        "初始化 LLM: provider=%s model=%s base_url=%s",
        llm_settings.provider,
        llm_settings.model,
        llm_settings.base_url,
    )

    return ChatOpenAI(
        model=llm_settings.model,
        api_key=llm_settings.api_key,
        base_url=llm_settings.base_url,
        temperature=llm_settings.temperature,
        timeout=llm_settings.timeout,
        max_retries=llm_settings.max_retries,
    )


@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    """返回进程内共享的默认模型实例（惰性创建）。"""

    return build_llm()


def __getattr__(name: str) -> Any:
    """向后兼容旧的 ``from app.agent.model import llm`` 写法。

    仅在实际访问时才构建模型，避免模块导入期读取配置。
    """

    if name == "llm":
        return get_llm()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
