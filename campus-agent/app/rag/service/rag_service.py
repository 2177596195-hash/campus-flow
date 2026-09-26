"""RAG 服务：检索链路的对外门面。

职责边界：
- ``retrieve`` 只做检索，返回带来源信息的片段；
- ``answer`` 在检索结果之上调用模型生成回答，并保留引用来源。

本模块是 Agent 层唯一需要认识的 RAG 入口：knowledge 节点只依赖
``RAGService``，因此换向量库、换 Embedding、切换到 Milvus 都不影响图与节点。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Union

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from app.config.settings import RAGSettings, get_rag_settings
from app.rag.pipeline import ensure_knowledge_base
from app.rag.retriever import KnowledgeRetriever
from app.rag.schema import RAGAnswer, RetrievedChunk
from app.rag.service.prompts import (
    NO_CONTEXT_REPLY,
    RAG_FALLBACK_REPLY,
    RAG_SYSTEM_PROMPT,
    build_context,
)

logger = logging.getLogger(__name__)

#: 模型依赖形式：既可直接给模型实例，也可给惰性工厂（推荐，避免导入期读配置）
LLMProvider = Union[BaseChatModel, Callable[[], BaseChatModel]]


def resolve_default_llm() -> BaseChatModel:
    """解析进程内默认聊天模型。

    这里刻意使用**函数内延迟导入**：模型工厂当前位于 ``app.agent.model``，
    若在模块顶层导入，会形成
    ``app.agent.nodes.knowledge -> app.rag.service -> app.agent.model``
    与 ``app.agent.__init__ -> app.agent.graph -> ...`` 的导入环。
    后续把模型工厂下沉到基础设施层（如 ``app/infrastructure/model.py``）后，
    本函数可退化为顶层导入。
    """

    from app.agent.model import get_llm

    return get_llm()


class RAGService:
    """校园知识库检索问答服务。"""

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        llm: LLMProvider | None = None,
        *,
        history_window: int = 6,
    ) -> None:
        self._retriever = retriever
        self._llm = llm
        self._history_window = history_window

    # ------------------------------------------------------------------ 检索

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        """检索知识库，返回带来源信息的片段。"""

        return self._retriever.retrieve(query, top_k=top_k)

    # ------------------------------------------------------------------ 问答

    def answer(
        self,
        query: str,
        *,
        history: Sequence[AnyMessage] | None = None,
    ) -> RAGAnswer:
        """基于检索结果生成回答，并携带引用来源。

        Args:
            query: 用户问题原文，同时作为检索查询。
            history: 最近若干轮对话，用于理解「那第二学期呢」这类追问；
                检索本身始终只使用当前问题，避免历史污染召回结果。

        Returns:
            ``RAGAnswer``：回答正文 + 引用来源 + 命中数量。
            模型不可用时返回兜底文案，不抛异常。
        """

        question = query.strip()
        if not question:
            return RAGAnswer(query=query, answer="请先输入你的问题。")

        chunks = self.retrieve(question)
        if not chunks:
            logger.info("知识库无可用片段，返回兜底回复")
            return RAGAnswer(query=question, answer=NO_CONTEXT_REPLY)

        context = build_context(
            [(index, chunk.title, chunk.source, chunk.content) for index, chunk in enumerate(chunks, 1)]
        )

        messages: list[AnyMessage] = [SystemMessage(content=f"{RAG_SYSTEM_PROMPT}\n\n{context}")]
        messages.extend(self._trim_history(history))
        messages.append(HumanMessage(content=question))

        try:
            response = self._model().invoke(messages)
        except Exception:  # noqa: BLE001 - 模型异常不应中断对话链路
            logger.exception("RAG 生成回答失败")
            return RAGAnswer(
                query=question,
                answer=RAG_FALLBACK_REPLY,
                sources=tuple(chunk.as_source() for chunk in chunks),
                retrieved=len(chunks),
            )

        content = response.content
        answer = content if isinstance(content, str) else str(content)
        return RAGAnswer(
            query=question,
            answer=answer,
            sources=tuple(chunk.as_source() for chunk in chunks),
            retrieved=len(chunks),
        )

    # ------------------------------------------------------------------ 内部实现

    def _model(self) -> BaseChatModel:
        """解析本轮使用的模型：注入实例 > 注入工厂 > 全局默认模型。"""

        provider = self._llm
        if provider is None:
            return resolve_default_llm()

        if isinstance(provider, BaseChatModel):
            return provider
        return provider()

    def _trim_history(self, history: Sequence[AnyMessage] | None) -> list[AnyMessage]:
        """截取最近若干条消息，并把检索指令与闲聊历史隔离开。

        除 ``ToolMessage`` 之外，**带 ``tool_calls`` 的 ``AIMessage`` 也必须过滤**：
        它们与对应的 ``ToolMessage`` 是成对的。若只过滤 ``ToolMessage`` 而留下
        ``AIMessage(tool_calls=...)``，发给模型的历史里就会出现「孤儿 tool_calls」
        ——DeepSeek 收到没有对应工具结果的调用请求会直接返回 HTTP 400
        （典型触发路径：上一轮「查询考试」走 Tool Calling，本轮紧接着问知识库）。
        普通 ``AIMessage``（纯文本回复）不受影响，照常保留。
        """

        if not history or self._history_window <= 0:
            return []

        recent = list(history)[-self._history_window :]
        return [
            message
            for message in recent
            if isinstance(message, (HumanMessage, AIMessage))
            and isinstance(message.content, str)
            and not (isinstance(message, AIMessage) and message.tool_calls)
        ]


def build_rag_service(
    *,
    llm: LLMProvider | None = None,
    retriever: KnowledgeRetriever | None = None,
    settings: RAGSettings | None = None,
    auto_ingest: bool = True,
) -> RAGService:
    """装配 RAG 服务。

    Args:
        llm: 生成回答所需的模型（实例或工厂）；省略时在首次调用时解析默认模型。
        retriever: 检索器；省略时按配置构建并复用共享向量库。
        settings: RAG 配置；省略时读取全局配置。
        auto_ingest: 知识库为空时是否自动摄取。
    """

    rag_settings = settings or get_rag_settings()

    if auto_ingest:
        ensure_knowledge_base(settings=rag_settings)

    return RAGService(
        retriever or KnowledgeRetriever.from_settings(settings=rag_settings),
        llm,
        history_window=rag_settings.history_window,
    )


@lru_cache(maxsize=1)
def get_rag_service() -> RAGService:
    """返回进程内共享的 RAG 服务（惰性装配，导入期不读取配置、不建连接）。"""

    return build_rag_service()


__all__ = ["LLMProvider", "RAGService", "build_rag_service", "get_rag_service", "resolve_default_llm"]
