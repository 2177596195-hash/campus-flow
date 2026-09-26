"""工具调用（Tool Calling）Agent 节点。

职责边界：本节点只做四件事——

1. 把消息历史（前置一段系统提示）交给**已绑定工具**的模型；
2. 把模型返回的 ``AIMessage``（可能携带 ``tool_calls``）写回状态；
3. 模型调用失败时降级为提示文本，不中断整轮对话；
4. 把上一轮补齐的任务槽位值（``pending_value``）作为一句上下文附加给模型，
   并在本次调用后消费掉它。

第 4 条是「多轮任务状态」在本节点的唯一落点：``task_slot`` 节点已经确定了
「本轮要查的是哪个学期的成绩」这个事实，但**不代替模型决定调不调工具**——
模型仍然自己产出 ``tool_calls``、自己填参数，工具仍由官方 ``ToolNode`` 执行。
这段上下文只随请求发出、不写回状态，因此不会在 checkpointer 里逐轮堆积。

工具真正执行由 LangGraph 官方 ``ToolNode`` 承担（见 ``app/agent/graph/graph.py``），
本节点不执行工具、不做任何字符串解析，只负责「让模型看到工具并产出调用意图」。
因此链路是标准 ReAct 循环::

    agent --(有 tool_calls)--> tools --> agent --> ... --> END
       \\--(无 tool_calls)----------------------------> END

工具调用意图以 LangChain 原生的 ``AIMessage.tool_calls`` 结构化字段表达，
执行结果以原生 ``ToolMessage`` 回到消息历史，**不额外复制一套 messages**。

RAG 与 Tool Calling 是两条互不依赖的能力：
``knowledge`` 意图走 RAG 节点，``query`` / ``action`` 走本节点。
本模块不 import 任何向量库 / Embedding / RAG 清单模块（由测试守卫强制）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool

from app.agent.graph.state import AgentState
from app.agent.model import get_llm
from app.agent.slots import slot_hint
from app.tools import get_course_schedule, get_exam_schedule, get_score

logger = logging.getLogger(__name__)

#: 模型服务不可用时的兜底回复
TOOL_AGENT_FALLBACK_REPLY: str = "抱歉，工具调用服务暂时不可用，请稍后重试。"

#: 消息历史为空时的兜底回复
EMPTY_MESSAGES_REPLY: str = "请先输入你想查询的内容。"

#: 工具调用分支的系统提示。通过工具描述以外的语气约束降低「凭记忆编造数据」的概率。
TOOL_AGENT_SYSTEM_PROMPT: str = (
    "你是校园事务助手，可以调用工具查询校园数据。\n"
    "当用户询问个人或教务数据（考试安排、成绩、课表等）时，"
    "必须调用相应工具获取真实数据，不要凭记忆编造。\n"
    "一个问题可能同时涉及多项数据，需要时请在同一轮里一次性调用所有相关工具"
    "（例如同时问课表和考试，就同时调用查课表与查考试的工具）。\n"
    "拿到工具结果后用简洁的中文回答用户，并说明数据来源。\n"
    "若没有任何工具能回答该问题，直接告知用户你无法查询该项数据。"
)

#: 本分支暴露给模型的工具集合。
#: 新增工具时在此登记即可（图侧与节点侧共用同一份清单，避免两处漂移）：
#: ``build_graph`` 默认取本元组，且同一个元组同时用于 ``bind_tools`` 与 ``ToolNode``，
#: 因此模型「看得到的工具」与「能被执行的工具」永远一致。
AGENT_TOOLS: Final[tuple[BaseTool, ...]] = (
    get_exam_schedule,
    get_course_schedule,
    get_score,
)

ToolAgentNode = Callable[[AgentState, Optional[RunnableConfig]], dict[str, Any]]


def tool_call_names(messages: Iterable[AnyMessage]) -> tuple[str, ...]:
    """按顺序收集若干消息中请求调用的工具名。

    兼容 ``tool_calls`` 中元素的两种形态（``dict`` 与对象），
    供节点日志与 CLI 展示复用，避免各处各自解析。
    """

    names: list[str] = []
    for message in messages:
        calls = getattr(message, "tool_calls", None) or ()
        for call in calls:
            name = call.get("name") if isinstance(call, Mapping) else getattr(call, "name", None)
            if name:
                names.append(str(name))
    return tuple(names)


def handle_tool_error(exc: Exception) -> str:
    """把工具异常转成可读的 ``ToolMessage`` 内容，并留下完整堆栈。

    为什么必须显式接管：``ToolNode`` 的默认策略只把 ``ToolInvocationError``
    转成消息，**其余异常会直接向上抛出并中断整张图**。工具是最外层的不稳定边界
    （网络、鉴权、数据格式），一次失败不应该让整轮对话崩掉，因此这里统一兜底：
    记录堆栈，并把失败原因作为 error 状态的 ``ToolMessage`` 交回模型，
    由模型用自然语言向用户解释。
    """

    logger.warning("工具执行失败: %s: %s", type(exc).__name__, exc, exc_info=True)
    return f"工具执行失败（{type(exc).__name__}）: {exc}"


def bind_tools(model: BaseChatModel, tools: Sequence[BaseTool]) -> Runnable[Any, Any]:
    """把工具绑定到模型，返回可直接 ``invoke`` 的 Runnable。

    使用 LangChain 原生 ``bind_tools``：工具的名称、描述与参数 schema
    会随请求一并发送给模型，模型以 ``tool_calls`` 字段表达调用意图，
    因此**不需要任何字符串匹配或正则模拟**。

    若模型自身不支持工具绑定（``BaseChatModel`` 基类默认抛
    ``NotImplementedError``），降级为纯对话模型而不是让整条链路失败。
    """

    if not tools:
        return model

    try:
        return model.bind_tools(list(tools))
    except NotImplementedError:
        logger.warning("模型 %s 不支持工具绑定，query/action 分支将退化为纯对话", type(model).__name__)
        return model
    except Exception:  # noqa: BLE001 - 供应商能力差异，降级而非中断
        logger.warning("工具绑定失败，query/action 分支将退化为纯对话", exc_info=True)
        return model


def _with_system_prompt(
    messages: list[AnyMessage], extra: str | None = None
) -> list[AnyMessage]:
    """把工具调用系统提示置于消息最前；``extra`` 为一句可选的任务上下文。

    系统提示**不写回状态**，因此不会随多轮会话在 checkpointer 里逐轮堆积；
    已存在同样提示时（例如调用方自行注入）不重复插入。

    ``extra`` 省略时返回值与既有行为**逐字节相同**——正常单轮链路不受影响。
    """

    prompt = TOOL_AGENT_SYSTEM_PROMPT if not extra else f"{TOOL_AGENT_SYSTEM_PROMPT}\n{extra}"
    if (
        messages
        and isinstance(messages[0], SystemMessage)
        and messages[0].content == prompt
    ):
        return messages
    return [SystemMessage(content=prompt), *messages]


def create_tool_agent_node(
    llm: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
) -> ToolAgentNode:
    """构建工具调用 Agent 节点。

    Args:
        llm: 注入的聊天模型；为 ``None`` 时在节点首次执行时解析默认模型，
            保证图构建阶段不读取配置、不建连接。
        tools: 暴露给模型的工具；为 ``None`` 时使用 ``AGENT_TOOLS``。

    Returns:
        符合 LangGraph 节点签名的可调用对象。
    """

    injected_llm = llm
    tool_list: tuple[BaseTool, ...] = tuple(AGENT_TOOLS if tools is None else tools)
    #: 绑定后的模型只解析一次并复用（bind_tools 的结果是 Runnable，无需重复构建）
    resolved: Runnable[Any, Any] | None = None

    def tool_agent_node(
        state: AgentState, config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        """让模型基于当前消息历史决定是否调用工具。

        注：``config`` 的注解需保持 ``Optional[RunnableConfig]`` 这一写法，
        LangGraph 的节点签名校验按字符串比对，使用 ``RunnableConfig | None``
        会触发 UserWarning。
        """

        nonlocal resolved

        messages: list[AnyMessage] = list(state.get("messages") or [])
        if not messages:
            logger.warning("工具调用节点收到空消息列表，跳过模型调用")
            return {
                "messages": [AIMessage(content=EMPTY_MESSAGES_REPLY)],
                "error": None,
                "pending_value": None,
            }

        # 上一轮补齐的槽位值：渲染成一句上下文附加到本次请求，并**在本次调用后
        # 消费掉**（无论成败）。一次性语义是刻意的——否则这个值会留在
        # checkpointer 里，之后每一轮都会被当成新提示重复注入。
        hint = slot_hint(state.get("pending_value"))
        if hint:
            logger.info("注入任务上下文: pending_value=%s", state.get("pending_value"))

        try:
            if resolved is None:
                resolved = bind_tools(injected_llm or get_llm(), tool_list)
            response = resolved.invoke(_with_system_prompt(messages, hint), config=config)
        except Exception as exc:  # 模型服务或网络异常：记录堆栈并降级返回
            logger.exception("工具调用 Agent 调用模型失败")
            return {
                "messages": [AIMessage(content=TOOL_AGENT_FALLBACK_REPLY)],
                "error": f"{type(exc).__name__}: {exc}",
                "pending_value": None,
            }

        requested = tool_call_names([response])
        if requested:
            logger.info("模型请求调用工具: %s", ", ".join(requested))
        else:
            logger.info("模型未请求工具，直接作答")

        return {"messages": [response], "error": None, "pending_value": None}

    return tool_agent_node


__all__ = [
    "AGENT_TOOLS",
    "EMPTY_MESSAGES_REPLY",
    "TOOL_AGENT_FALLBACK_REPLY",
    "TOOL_AGENT_SYSTEM_PROMPT",
    "ToolAgentNode",
    "bind_tools",
    "create_tool_agent_node",
    "handle_tool_error",
    "tool_call_names",
]
