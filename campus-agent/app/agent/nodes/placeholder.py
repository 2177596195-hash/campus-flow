"""占位节点：承接本阶段尚未实现的意图分支。

目的：让 Graph 拓扑在接入 RAG / Tool Calling / MCP 之前就已是完整形态。
后续把某个分支替换为真实节点时，只需改 ``graph.py`` 中对应的 ``add_node``
工厂，条件边映射与其余分支保持不变。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.agent.graph.state import AgentState
from app.agent.intent import Intent

logger = logging.getLogger(__name__)

PlaceholderNode = Callable[[AgentState, Optional[RunnableConfig]], dict[str, Any]]

_REPLY_TEMPLATE = "[{intent}] 该意图分支的拓扑已就绪，具体能力将在后续阶段接入。"


def create_placeholder_node(intent: Intent) -> PlaceholderNode:
    """为指定意图构建占位节点。"""

    reply = _REPLY_TEMPLATE.format(intent=intent)

    def placeholder_node(
        state: AgentState, config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        logger.info("意图 %s 命中占位节点", intent)
        return {"messages": [AIMessage(content=reply)], "error": None}

    return placeholder_node
