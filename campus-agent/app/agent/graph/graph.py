"""Agent Runtime 图定义。

本文件只描述拓扑：状态结构 + 节点注册 + 条件边连接。
模型、意图识别器、RAG 服务、工具集、检查点存储器等外部依赖全部由调用方注入，
保证图定义与基础设施解耦，便于测试与后续按需扩展。

当前拓扑::

    START -> intent_router -> task_slot -> (条件路由) -> chat / knowledge / agent / unknown -> END
                                              |-> END（本轮只输出一句追问）
                                                     agent <-> tools（ReAct 回边）

``task_slot`` 是「多轮任务状态」的入口：它读待办槽位（``pending_*``）、能补就补、
缺就追问，并在「补上了」的时候把意图恢复成**待办任务的**意图再交给下游。
因此本图的条件边源从 ``intent_router`` 移到了它后面——但意图仍然只是被判定、
路由仍然只由条件边决定，两个节点的职责边界没有变。

各分支对应关系：

- ``chat``              纯对话节点
- ``knowledge``         RAG 知识问答节点（检索链路）
- ``query`` / ``action`` -> ``agent`` 工具调用节点（Tool Calling）
- ``unknown``           占位节点（保持既有行为）

Tool Calling 分支是标准 ReAct 循环：``agent`` 节点让模型产出
``AIMessage.tool_calls``，条件边据此决定进入官方 ``ToolNode`` 执行工具，
执行结果以 ``ToolMessage`` 回到消息历史后再次进入 ``agent``，直到模型不再请求工具。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.graph.state import AgentState
from app.agent.intent import DEFAULT_INTENT, Intent, IntentClassifier
from app.agent.nodes.chat import create_chat_node
from app.agent.nodes.intent_router import create_intent_router_node
from app.agent.nodes.knowledge import create_knowledge_node
from app.agent.nodes.placeholder import create_placeholder_node
from app.agent.nodes.task_slot import create_task_slot_node
from app.agent.nodes.tool_agent import AGENT_TOOLS, create_tool_agent_node, handle_tool_error
from app.rag.service import RAGService

logger = logging.getLogger(__name__)

# 节点名常量，供条件边 / 后续路由节点引用，避免散落字符串字面量
INTENT_ROUTER_NODE: str = "intent_router"
#: 任务槽位节点：补齐上一轮缺的槽位，或为缺槽位的新请求写入待办并追问。
#: 条件边的源是它（而不是 ``intent_router``），因为「最终走哪个分支」要等
#: 待办任务被并进来才能定。
TASK_SLOT_NODE: str = "task_slot"
CHAT_NODE: str = "chat"
KNOWLEDGE_NODE: str = "knowledge"
#: 工具调用节点（query / action 共用）
AGENT_NODE: str = "agent"
#: 工具执行节点。名称取 LangGraph 约定值 ``tools``，与官方 ``tools_condition`` 的
#: 路由取值一致，因此条件边无需额外包装函数。
TOOLS_NODE: str = "tools"
#: 尚未实现的意图分支（保持既有占位行为）
UNKNOWN_NODE: str = "unknown"

#: intent -> 下游节点名。query 与 action 共用同一个 Tool Calling 分支。
INTENT_NODE_MAP: Final[dict[str, str]] = {
    "chat": CHAT_NODE,
    "knowledge": KNOWLEDGE_NODE,
    "query": AGENT_NODE,
    "action": AGENT_NODE,
    "unknown": UNKNOWN_NODE,
}

#: 条件边的路径登记表：``route_by_intent`` 返回的已经是**节点名**，
#: 因此这里登记去重后的全部去向，供 LangGraph 校验与绘图。
ROUTE_TARGETS: Final[dict[str, str]] = {
    node: node for node in dict.fromkeys(INTENT_NODE_MAP.values())
}

#: ``task_slot`` 之后条件边的路径登记表：比 ``ROUTE_TARGETS`` 多一个「结束」去向
#: （待办槽位未补齐时，本轮只有一句追问）。
SLOT_ROUTE_TARGETS: Final[dict[str, str]] = {**ROUTE_TARGETS, END: END}

#: 仍以占位节点承接的意图分支
PLACEHOLDER_INTENTS: Final[tuple[Intent, ...]] = ("unknown",)


def route_by_intent(state: AgentState) -> str:
    """条件边函数：把 ``state["intent"]`` 映射为下游节点名。

    取值缺失或非法时回退到 ``unknown`` 分支，保证图不会因状态异常而中断。
    """

    intent = state.get("intent")
    node = INTENT_NODE_MAP.get(intent) if intent else None
    if node is None:
        logger.warning("intent 取值异常(%r)，回退到 %s 分支", intent, INTENT_NODE_MAP[DEFAULT_INTENT])
        return INTENT_NODE_MAP[DEFAULT_INTENT]
    return node


def route_after_task_slot(state: AgentState) -> str:
    """条件边函数：待办槽位仍未补齐时结束本轮，否则按意图路由。

    ``pending_slot`` 非空 恒等于「正在等用户补一个槽位」（``task_slot`` 补齐之后
    会把它置回 ``None``），此时本轮的产出就只有那句追问，直接结束；否则路由交给
    ``route_by_intent``，由 ``intent`` 决定下游分支。
    """

    pending_slot = state.get("pending_slot")
    if pending_slot:
        logger.info("待办槽位 %s 未补齐，本轮只输出追问", pending_slot)
        return END
    return route_by_intent(state)


def build_graph(
    llm: BaseChatModel | None = None,
    intent_classifier: IntentClassifier | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    rag_service: RAGService | None = None,
    tools: Sequence[BaseTool] | None = None,
) -> CompiledStateGraph:
    """构建并编译 Agent Runtime 图。

    Args:
        llm: 注入的聊天模型；``None`` 时由 chat / agent 节点在运行期解析默认模型。
        intent_classifier: 注入的意图识别器；``None`` 时由路由节点在运行期解析。
        checkpointer: 会话检查点存储器。传入后即可按 ``thread_id`` 保留多轮状态，
            也是 Human-in-the-loop 的前置条件；``None`` 时为无状态单轮模式。
        rag_service: 注入的 RAG 服务；``None`` 时由 knowledge 节点在运行期解析，
            因此图构建阶段不会初始化 Embedding 与向量库。
        tools: 暴露给模型的工具集；``None`` 时使用 ``AGENT_TOOLS``。

    Returns:
        可直接 ``invoke`` / ``ainvoke`` 的已编译图。
    """

    tool_list: tuple[BaseTool, ...] = tuple(AGENT_TOOLS if tools is None else tools)

    builder = StateGraph(AgentState)

    builder.add_node(INTENT_ROUTER_NODE, create_intent_router_node(intent_classifier))
    # 多轮任务状态：意图判定之后、分支跳转之前，把待办任务与本轮输入对上
    builder.add_node(TASK_SLOT_NODE, create_task_slot_node())
    builder.add_node(CHAT_NODE, create_chat_node(llm))
    # knowledge 分支接入真实 RAG 链路
    builder.add_node(KNOWLEDGE_NODE, create_knowledge_node(rag_service))
    # query / action 分支接入 Tool Calling：agent 决策 -> tools 执行 -> agent 汇总
    builder.add_node(AGENT_NODE, create_tool_agent_node(llm, tool_list))
    builder.add_node(
        TOOLS_NODE,
        ToolNode(tool_list, name=TOOLS_NODE, handle_tool_errors=handle_tool_error),
    )
    for intent in PLACEHOLDER_INTENTS:
        builder.add_node(INTENT_NODE_MAP[intent], create_placeholder_node(intent))

    builder.add_edge(START, INTENT_ROUTER_NODE)
    # 意图先在 router 里判定（节点只写 state["intent"]），再由 task_slot 结合
    # 待办槽位把「本轮真正要做的事」定下来，最后由条件边跳转。
    builder.add_edge(INTENT_ROUTER_NODE, TASK_SLOT_NODE)
    builder.add_conditional_edges(TASK_SLOT_NODE, route_after_task_slot, SLOT_ROUTE_TARGETS)

    # agent 之后的分叉：模型请求了工具就进 ToolNode，否则直接结束。
    # 官方 ``tools_condition`` 只看最后一条消息的 ``tool_calls``，
    # 取值固定为 ``"tools"`` / ``"__end__"``，此处映射到本图的节点名。
    builder.add_conditional_edges(
        AGENT_NODE, tools_condition, {TOOLS_NODE: TOOLS_NODE, END: END}
    )
    # 回边：工具执行结果以 ToolMessage 送回 agent，由模型生成最终自然语言回答
    builder.add_edge(TOOLS_NODE, AGENT_NODE)

    # 其余能力分支执行完即结束（agent 分支已由上面的条件边决定去向，不能再加固定边）
    for node_name in (CHAT_NODE, KNOWLEDGE_NODE, UNKNOWN_NODE):
        builder.add_edge(node_name, END)

    return builder.compile(checkpointer=checkpointer)


# 默认运行时实例：仅完成拓扑编译，不触发模型、意图识别器与配置解析。
# 注意：该实例不带 checkpointer；需要多轮会话时用 build_graph(checkpointer=...) 单独装配。
agent_graph: CompiledStateGraph = build_graph()
