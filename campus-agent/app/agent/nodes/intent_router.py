"""意图路由节点。

职责边界：读取最新一条用户消息 -> 调用识别器 -> 把结果写入 ``state["intent"]``。
本节点不做分支跳转（跳转由 ``graph.py`` 的条件边决定），也不执行业务逻辑，
从而与下游各能力节点彻底解耦。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from app.agent.graph.state import AgentState
from app.agent.intent import DEFAULT_INTENT, IntentClassifier, get_intent_classifier
from app.agent.run_context import annotate_intent

logger = logging.getLogger(__name__)

IntentRouterNode = Callable[[AgentState, Optional[RunnableConfig]], dict[str, Any]]


def latest_user_text(state: AgentState) -> str:
    """取出最近一条用户消息的文本内容。"""

    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def create_intent_router_node(classifier: IntentClassifier | None = None) -> IntentRouterNode:
    """构建意图路由节点。

    Args:
        classifier: 注入的意图识别器；为 ``None`` 时在节点首次执行时
            解析进程内默认识别器，保证图构建阶段不触发配置读取。

    Returns:
        符合 LangGraph 节点签名的可调用对象。
    """

    injected_classifier = classifier

    def intent_router_node(
        state: AgentState, config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        """识别当前输入意图并写回状态。

        识别完成后额外做一件事：把意图以 ``intent:<取值>`` 追加进本轮的 run tags。
        意图只有这里才知道，而 tags 会被 LangGraph 按引用传给后续 super-step，
        因此 router 之后的所有 run（分支节点、模型、工具）都能带上意图标签。
        这一步不改状态、不改路由、不重跑识别，对业务结果零影响。
        """

        text = latest_user_text(state)

        try:
            active_classifier = injected_classifier or get_intent_classifier()
            intent = active_classifier.classify(text)
        except Exception as exc:  # noqa: BLE001 - 路由失败不应中断整轮对话
            logger.exception("意图识别失败，回退为 %s", DEFAULT_INTENT)
            annotate_intent(config, DEFAULT_INTENT)
            return {"intent": DEFAULT_INTENT, "error": f"{type(exc).__name__}: {exc}"}

        logger.info("意图识别完成: intent=%s input=%r", intent, text[:60])
        annotate_intent(config, intent)
        return {"intent": intent, "error": None}

    return intent_router_node
