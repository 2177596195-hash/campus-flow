"""知识问答节点：把 knowledge 意图接入 RAG 链路。

职责边界：节点只做三件事——
1. 从消息历史取出当前问题；
2. 交给 ``RAGService``；
3. 把回答与引用来源写回状态。

检索细节（向量库、Embedding、top_k）全部封装在 RAG 模块内，
本节点不 import 任何向量库相关模块，因此换后端无需改动节点。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Optional

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from app.agent.graph.state import AgentState
from app.rag.schema import RAGAnswer
from app.rag.service import RAGService, RAG_FALLBACK_REPLY, get_rag_service

logger = logging.getLogger(__name__)

KnowledgeNode = Callable[[AgentState, Optional[RunnableConfig]], dict[str, Any]]

#: 取不到用户问题时的兜底回复
EMPTY_QUERY_REPLY: str = "请先输入你想了解的校园事务问题。"


def _split_question(messages: Sequence[AnyMessage]) -> tuple[str, list[AnyMessage]]:
    """切分出「当前问题」与「其之前的历史消息」。

    当前问题 = 最后一条用户消息；历史 = 它之前的全部消息（用于理解追问）。
    这样即使此前多轮状态被 checkpointer 累积，也只把历史交给模型，
    不把当前问题重复塞进上下文。
    """

    last_user_index = max(
        (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=-1,
    )
    if last_user_index < 0:
        return "", list(messages)

    content = messages[last_user_index].content
    question = content if isinstance(content, str) else str(content)
    return question, list(messages[:last_user_index])


def create_knowledge_node(service: RAGService | None = None) -> KnowledgeNode:
    """构建知识问答节点。

    Args:
        service: 注入的 RAG 服务；为 ``None`` 时在节点运行期解析默认服务，
            保证图构建阶段不触发 Embedding / 向量库初始化。

    Returns:
        符合 LangGraph 节点签名的可调用对象。
    """

    injected_service = service

    def knowledge_node(
        state: AgentState, config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        """检索知识库并生成回答。

        注：``config`` 的注解需保持 ``Optional[RunnableConfig]`` 这一写法，
        LangGraph 的节点签名校验按字符串比对，使用 ``RunnableConfig | None``
        会触发 UserWarning。
        """

        messages: list[AnyMessage] = list(state.get("messages") or [])
        question, history = _split_question(messages)
        if not question.strip():
            logger.warning("knowledge 节点未取到用户问题")
            return {"messages": [AIMessage(content=EMPTY_QUERY_REPLY)], "sources": [], "error": None}

        try:
            rag_service = injected_service or get_rag_service()
            result: RAGAnswer = rag_service.answer(question, history=history)
        except Exception as exc:  # noqa: BLE001 - 检索/模型异常降级为提示，不中断对话
            logger.exception("知识库问答失败")
            return {
                "messages": [AIMessage(content=RAG_FALLBACK_REPLY)],
                "sources": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

        logger.info("知识库问答完成: 命中片段 %d 个", result.retrieved)
        return {
            "messages": [AIMessage(content=result.answer)],
            "sources": list(result.sources),
            "error": None,
        }

    return knowledge_node


__all__ = ["EMPTY_QUERY_REPLY", "KnowledgeNode", "create_knowledge_node"]
