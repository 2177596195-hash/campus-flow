"""CampusFlow Agent 运行时包。

对外暴露稳定边界：状态契约、图构建入口、会话检查点、模型工厂与意图契约。
内部实现（节点细节、拓扑组织）不在此处导出，便于后续独立演进。
"""

from app.agent.graph import (
    AgentState,
    SQLITE_BACKEND,
    agent_graph,
    build_graph,
    close_checkpointer,
    create_checkpointer,
    supported_backends,
)
from app.agent.intent import DEFAULT_INTENT, INTENTS, Intent, IntentClassifier
from app.agent.model import build_llm, get_llm
from app.agent.run_context import (
    ENTRYPOINT_CLI_REPL,
    ENTRYPOINT_CLI_SINGLE,
    INTENT_TAGS,
    RUN_NAME,
    annotate_intent,
    build_run_context,
    intent_tag,
)

__all__ = [
    "DEFAULT_INTENT",
    "ENTRYPOINT_CLI_REPL",
    "ENTRYPOINT_CLI_SINGLE",
    "INTENTS",
    "INTENT_TAGS",
    "RUN_NAME",
    "SQLITE_BACKEND",
    "AgentState",
    "Intent",
    "IntentClassifier",
    "agent_graph",
    "annotate_intent",
    "build_graph",
    "build_llm",
    "build_run_context",
    "close_checkpointer",
    "create_checkpointer",
    "get_llm",
    "intent_tag",
    "supported_backends",
]
