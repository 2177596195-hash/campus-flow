"""对话节点：Agent Runtime 的最小可运行单元。

节点通过工厂函数构建，模型对象由外部注入；模型解析推迟到节点首次执行，
因此模块导入与图构建阶段不依赖任何环境变量。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig

from app.agent.graph.state import AgentState
from app.agent.model import get_llm

logger = logging.getLogger(__name__)

# 模型调用失败时的兜底回复
FALLBACK_REPLY: str = "抱歉，模型服务暂时不可用，请稍后重试。"

ChatNode = Callable[[AgentState, Optional[RunnableConfig]], dict[str, Any]]


def create_chat_node(llm: BaseChatModel | None = None) -> ChatNode:
    """构建对话节点。

    Args:
        llm: 注入的聊天模型；为 ``None`` 时在节点运行期解析默认模型。

    Returns:
        符合 LangGraph 节点签名的可调用对象。
    """

    injected_llm = llm

    def chat_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict[str, Any]:
        """将当前消息历史交给模型并追加其回复。

        注：``config`` 的注解需保持 ``Optional[RunnableConfig]`` 这一写法，
        LangGraph 的节点签名校验按字符串比对，使用 ``RunnableConfig | None``
        会触发 UserWarning。
        """

        messages: list[AnyMessage] = list(state.get("messages") or [])
        if not messages:
            logger.warning("chat 节点收到空消息列表，跳过模型调用")
            return {"messages": [AIMessage(content="请先输入你的问题。")], "error": None}

        model = injected_llm or get_llm()
        try:
            response = model.invoke(messages, config=config)
        except Exception as exc:  # 模型服务或网络异常：记录完整堆栈并降级返回
            logger.exception("调用大模型失败")
            return {
                "messages": [AIMessage(content=FALLBACK_REPLY)],
                "error": f"{type(exc).__name__}: {exc}",
            }

        return {"messages": [response], "error": None}

    return chat_node


def __getattr__(name: str) -> Any:
    """向后兼容旧的 ``from app.agent.nodes.chat import chat_node`` 写法。"""

    if name == "chat_node":
        return create_chat_node()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
