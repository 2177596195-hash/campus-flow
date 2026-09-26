"""Tool Calling 链路测试（完全离线）。

覆盖目标链路::

    intent_router -> (query/action) -> agent -> [tools -> agent] -> END

核心手法是用 ``ScriptedChatModel`` 确定性地产生 ``AIMessage(tool_calls=[...])``，
因此 **Agent -> ToolNode -> Agent** 的每一步都可在无网络、无真实模型的前提下断言，
不依赖真实 LLM 才能验证核心逻辑。

真实 DeepSeek/Qwen 的工具调用不放在 pytest 里（会引入网络依赖与不确定性），
而是通过 ``python main.py "..."`` 做一次集成验证。

多工具部分（``AGENT_TOOLS`` = 考试安排 / 课程表 / 成绩）额外覆盖：

- 三个工具都被绑定给模型，且**只有一份**工具清单（图侧不重定义工具名）；
- 模型请求哪个工具就执行哪个，参数被原样传递；
- 一条 ``AIMessage`` 里的多个 ``tool_calls`` 全部被执行，``ToolMessage`` 数量正确；
- 跨轮连续调用两个工具（``agent -> tools -> agent -> tools -> agent``）；
- 最终回答建立在**全部**工具结果之上；
- 单个工具失败（含真实工具的取值校验失败）被隔离，不影响同批次其它工具；
- 模型可据错误消息自行改参重试。

本文件同时守住三条边界：
1. RAG 与 Tool Calling 相互独立（knowledge 分支不得触碰模型与工具）；
2. chat / unknown 分支行为不变；
3. 工具调用全部复用原生消息结构（不额外复制一套 messages）。

``AgentState`` 的**多轮任务上下文槽位**（``pending_intent`` / ``pending_slot`` /
``pending_value``）由 ``tests/agent/test_task_state.py`` 覆盖；本文件只守住
「这些槽位没有被工具调用链路滥用」——工具调用只用原生 ``tool_calls`` /
``ToolMessage`` 表达。

**关于三个工具的 HTTP 边界（接口变化说明）**

三个查询工具的数据来源都已从「模块内写死的示例数据」改为
「经 ``CampusApiClient`` 调用 Spring Boot 服务，由服务访问 MySQL」：

- ``get_exam_schedule`` → ``GET /api/exams``；
- ``get_course_schedule`` → ``GET /api/courses``；
- ``get_score`` → ``GET /api/scores``。

工具的**签名与返回结构完全未变**，因此本文件绝大多数断言无需改动；
变化的是「数据从哪来」——它现在是一条真实的外部依赖，因此断言里的具体数值
（课程名、分数）必须与 MySQL 种子数据保持一致，见 ``tests/_fakes.py``。

于是本文件依赖新边界上的替身：``tests/conftest.py`` 提供的全局夹具
会把校园业务服务客户端替换成挂在 ``httpx.MockTransport`` 上的替身，
其响应内容与 MySQL 种子数据一致，并按**请求路径**分派三条接口。这样做的意义是：

- 用例仍然完全离线、确定性（不会因为本机是否开着 Spring Boot 而变化）；
- 断言的是**真实契约下的数据形态**，而不是为了测试方便虚构的结构；
- 需要模拟服务故障的用例（如 5xx / 超时 / 连接被拒）可以自行注入替身。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from app.agent.graph import AGENT_NODE, build_graph, route_by_intent
from app.agent.graph import graph as graph_module
from app.agent.graph.graph import CHAT_NODE, KNOWLEDGE_NODE, TOOLS_NODE, UNKNOWN_NODE
from app.agent.graph.state import AgentState
from app.agent.nodes import tool_agent as tool_agent_module
from app.agent.nodes.tool_agent import (
    AGENT_TOOLS,
    TOOL_AGENT_FALLBACK_REPLY,
    TOOL_AGENT_SYSTEM_PROMPT,
)
from app.tools import get_course_schedule, get_exam_schedule, get_score
from tests._fakes import (
    FakeRAGService,
    ScriptedChatModel,
    StaticIntentClassifier,
    make_campus_client,
    patch_campus_api,
)

EXAM_QUESTION = "我最近有什么考试？"
COURSE_QUESTION = "我这周有什么课？"
SCORE_QUESTION = "数据结构考了多少分？"

#: 注册进 ``AGENT_TOOLS`` 的工具名，顺序与注册顺序一致。
#: 新增工具时只改这一处，绝大多数多工具用例会自动覆盖到。
EXPECTED_TOOL_NAMES: list[str] = ["get_exam_schedule", "get_course_schedule", "get_score"]

# --------------------------------------------------------------- 测试用构造


def tool_call_message(
    name: str = "get_exam_schedule",
    *,
    call_id: str = "call-1",
    args: dict[str, Any] | None = None,
) -> AIMessage:
    """构造一条「请求调用工具」的 AI 消息（原生 ``tool_calls`` 结构）。"""

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}],
    )


def tool_call_messages(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    """构造一条携带**多个** ``tool_calls`` 的 AI 消息（模拟模型一次调用多个工具）。

    调用序号 ``call-1`` / ``call-2`` … 与参数顺序一一对应，便于断言「哪个结果
    属于哪次调用」。**不依赖** ``ToolNode`` 返回 ``ToolMessage`` 的先后顺序：
    批量执行时按 ``tool_call_id`` 配对断言，避免并行实现差异带来的偶发失败。
    """

    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": f"call-{index}", "type": "tool_call"}
            for index, (name, args) in enumerate(calls, start=1)
        ],
    )


def build_test_graph(
    *,
    intent: str = "query",
    llm: ScriptedChatModel | None = None,
    tools: list[Any] | None = None,
    rag_service: FakeRAGService | None = None,
    checkpointer: Any = None,
):
    """装配被测图：模型 / 意图识别器 / RAG 全部为替身，全程不触网。"""

    return build_graph(
        llm=llm or ScriptedChatModel.scripted(AIMessage(content="默认回复")),
        intent_classifier=StaticIntentClassifier(intent),  # type: ignore[arg-type]
        rag_service=rag_service or FakeRAGService(),  # type: ignore[arg-type]
        checkpointer=checkpointer,
        tools=tools,
    )


def invoke(graph: Any, text: str, thread_id: str = "t-1") -> dict[str, Any]:
    """执行一轮对话（统一带上 thread_id，便于会话隔离用例复用）。"""

    return graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [message for message in state["messages"] if isinstance(message, ToolMessage)]


def human_texts(state: dict[str, Any]) -> list[str]:
    return [
        str(message.content)
        for message in state["messages"]
        if isinstance(message, HumanMessage)
    ]


class EchoToolResultModel(ScriptedChatModel):
    """把收到的**全部**工具结果回显进回答的脚本模型。

    用它可以做**有意义的「回答基于工具结果」断言**：最终回答里必须出现
    工具真实返回的字段值，而不是脚本里预先写死的文本。

    ``first_request`` 决定首轮请求调用哪些工具（默认只请求考试安排）；
    传入 ``tool_call_messages(...)`` 即可构造「一轮请求多个工具」的场景，
    从而断言多个工具返回的数据都进入了最终作答。
    """

    #: 尚无工具结果时发出的「请求调用工具」消息
    first_request: AIMessage = Field(default_factory=tool_call_message)

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return ChatResult(generations=[ChatGeneration(message=self.first_request)])
        joined = " | ".join(str(message.content) for message in results)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=f"查询结果：{joined}"))]
        )


class ExplodingChatModel(ScriptedChatModel):
    """调用即失败的模型替身，用于验证模型异常时的降级路径。"""

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        raise RuntimeError("模型服务 503")


@tool
def broken_tool() -> str:
    """总是抛出异常的工具（测试用）。"""

    raise RuntimeError("下游服务不可用")


@tool
def echo_args(week: int | None = None, course: str | None = None) -> str:
    """把收到的参数原样回显（测试用）。

    为什么需要它：真实工具的返回值无法区分「参数传对了」与「参数被忽略了」。
    这个回显工具能让「参数传递」成为可精确断言的等式。
    """

    return json.dumps({"week": week, "course": course}, ensure_ascii=False)


# ------------------------------------------- 1 / 2. query 与 action 都进 agent


@pytest.mark.parametrize("intent", ["query", "action"])
def test_query_and_action_enter_tool_agent_branch(intent: str) -> None:
    """query / action 两个意图都进入 Tool Calling Agent 分支。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="你最近有两门考试。"))
    graph = build_test_graph(intent=intent, llm=llm)

    state = invoke(graph, EXAM_QUESTION)

    assert state["intent"] == intent
    assert state["messages"][-1].content == "你最近有两门考试。"
    # 模型确实被调用，并且看到了**全部**已登记工具（说明绑定发生在 agent 分支）
    assert llm.calls == 1
    assert llm.bound_tool_names == EXPECTED_TOOL_NAMES


@pytest.mark.parametrize(
    ("intent", "expected_node"),
    [
        ("query", AGENT_NODE),
        ("action", AGENT_NODE),
        ("chat", CHAT_NODE),
        ("knowledge", KNOWLEDGE_NODE),
        ("unknown", UNKNOWN_NODE),
        ("不存在", UNKNOWN_NODE),
    ],
)
def test_route_by_intent_still_maps_branches(intent: str, expected_node: str) -> None:
    """条件边仍然按 ``INTENT_NODE_MAP`` 分发；非法取值回退到 unknown。"""

    assert route_by_intent({"intent": intent}) == expected_node  # type: ignore[arg-type]


def test_agent_state_contract_shape() -> None:
    """``AgentState`` 的字段集合是显式契约：只允许按需增删，不接受悄悄变形。

    本阶段新增了三个「多轮任务上下文」槽位（见 ``tests/agent/test_task_state.py``）；
    除此之外字段集合与工具调用阶段完全一致——工具调用本身仍然只用原生
    ``tool_calls`` / ``ToolMessage`` 表达，没有为它新增任何状态字段。
    """

    assert set(AgentState.__annotations__) == {
        "messages",
        "intent",
        "session_id",
        "user_id",
        "error",
        "sources",
        "pending_intent",
        "pending_slot",
        "pending_value",
    }


# --------------------------------------------- 3. 不需要工具：一轮即结束


def test_llm_without_tool_call_ends_directly() -> None:
    """模型未请求工具时，agent 之后直接结束，不产生任何 ToolMessage。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="你好，我可以帮你查询考试安排。"))
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "你好")

    assert llm.calls == 1
    assert tool_messages(state) == []
    assert len(state["messages"]) == 2  # Human + AI
    assert state["messages"][-1].content == "你好，我可以帮你查询考试安排。"


def test_system_prompt_is_injected_but_not_persisted() -> None:
    """系统提示只随请求发出，不写回状态（避免多轮累积污染 checkpointer）。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="好的"))
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "你好")

    assert isinstance(llm.received[0][0], SystemMessage)
    assert llm.received[0][0].content == TOOL_AGENT_SYSTEM_PROMPT
    assert not any(isinstance(m, SystemMessage) for m in state["messages"])


# ------------------------------- 4 / 5. 工具被真正执行，结果回流到模型


def test_llm_tool_call_is_executed_by_tool_node() -> None:
    """模型请求 get_exam_schedule 时，由官方 ToolNode 真正执行工具。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message(),
        AIMessage(content="你最近有计算机网络、数据结构两门考试。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, EXAM_QUESTION)

    assert llm.calls == 2  # agent -> tools -> agent
    executed = tool_messages(state)
    assert len(executed) == 1
    message = executed[0]
    assert message.name == "get_exam_schedule"
    assert message.status == "success"
    assert message.tool_call_id == "call-1"
    # 工具真正执行过：内容里是服务返回的数据，而非模型编造
    assert "计算机网络" in message.content
    assert "数据结构" in message.content
    assert "A302" in message.content
    assert "2026-09-28" in message.content


def test_tool_result_flows_back_to_the_model() -> None:
    """第二次模型调用必须能看到 ToolMessage 与原始的 tool_calls。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message(),
        AIMessage(content="你最近有计算机网络、数据结构两门考试。"),
    )
    graph = build_test_graph(llm=llm)

    invoke(graph, EXAM_QUESTION)

    second_round = llm.received[1]
    assert isinstance(second_round[-1], ToolMessage)
    assert second_round[-1].name == "get_exam_schedule"
    # 消息序列合法：请求工具的 AIMessage 仍在上下文里，与 ToolMessage 成对出现
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in second_round)
    # 工具返回的结构化数据确实进入了模型上下文
    assert any("数据结构" in str(m.content) for m in second_round if isinstance(m, ToolMessage))


# ------------------------------------------- 6. 最终回答基于工具结果生成


def test_final_answer_is_grounded_in_tool_result() -> None:
    """最终回答由「看过工具结果」的那次调用产生，内容即工具真实返回的数据。"""

    llm = EchoToolResultModel()
    graph = build_test_graph(llm=llm)

    state = invoke(graph, EXAM_QUESTION)

    final = str(state["messages"][-1].content)
    assert final.startswith("查询结果：")
    # 回答里的信息只能来自工具返回值
    for expected in ("计算机网络", "数据结构", "2026-09-28", "A302"):
        assert expected in final
    assert llm.calls == 2


# ----------------------------------------------- 7. 工具异常与未知工具名


def test_tool_exception_becomes_error_message_not_crash() -> None:
    """工具抛异常时转成 error 状态的 ToolMessage，图继续跑完并给出解释。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message(name="broken_tool"),
        AIMessage(content="抱歉，考试安排暂时查询不到，请稍后重试。"),
    )
    graph = build_test_graph(llm=llm, tools=[broken_tool])

    state = invoke(graph, EXAM_QUESTION)

    executed = tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "工具执行失败" in executed[0].content
    assert "RuntimeError" in executed[0].content
    # 模型拿到了失败原因，并据此给出自然语言回答
    assert state["messages"][-1].content == "抱歉，考试安排暂时查询不到，请稍后重试。"
    assert llm.calls == 2


def test_unknown_tool_name_is_reported_back_to_model() -> None:
    """模型请求不存在的工具时，报错信息回流给模型而不是中断整张图。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message(name="no_such_tool"),
        AIMessage(content="我目前无法完成该查询。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, EXAM_QUESTION)

    executed = tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "no_such_tool" in executed[0].content
    assert state["messages"][-1].content == "我目前无法完成该查询。"


def test_llm_failure_degrades_without_crashing() -> None:
    """模型本身异常时降级为兜底文案，并把原因写入 state["error"]。"""

    graph = build_test_graph(llm=ExplodingChatModel())

    state = invoke(graph, EXAM_QUESTION)

    assert state["messages"][-1].content == TOOL_AGENT_FALLBACK_REPLY
    assert "RuntimeError" in str(state["error"])


def test_graph_wires_custom_tool_error_handler() -> None:
    """守卫：ToolNode 必须接自定义异常处理器。

    LangGraph 默认策略只吞 ``ToolInvocationError``，其余异常会冒出整张图；
    若这条接线被移除，上面的工具异常用例会立刻失败，这里再显式守住一次。
    """

    source = Path(graph_module.__file__).read_text(encoding="utf-8")

    assert "handle_tool_errors=handle_tool_error" in source


# ------------------------------------------- 8 / 9. 多轮对话与会话隔离


def test_multi_turn_conversation_accumulates_history() -> None:
    """启用 checkpointer 后，同一 thread_id 的多轮消息持续累积。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message(),
        AIMessage(content="你最近有计算机网络、数据结构两门考试。"),
        AIMessage(content="数据结构在 09-30 19:00，B201 考场。"),
    )
    graph = build_test_graph(llm=llm, checkpointer=InMemorySaver())

    first = invoke(graph, EXAM_QUESTION, "thread-a")
    assert len(tool_messages(first)) == 1

    second = invoke(graph, "那第二门呢？", "thread-a")

    assert human_texts(second) == [EXAM_QUESTION, "那第二门呢？"]
    assert second["messages"][-1].content == "数据结构在 09-30 19:00，B201 考场。"
    # 第二轮模型能看到第一轮的工具结果（历史随 thread 保留）
    assert any(isinstance(m, ToolMessage) for m in llm.received[-1])
    # 第二轮模型未请求工具，因此不会再执行一次
    assert llm.calls == 3


def test_thread_id_isolates_sessions() -> None:
    """不同 thread_id 的会话互不可见。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="回复1"), AIMessage(content="回复2"))
    graph = build_test_graph(llm=llm, checkpointer=InMemorySaver())

    invoke(graph, "会话A第一问", "thread-a")
    thread_a = invoke(graph, "会话A第二问", "thread-a")
    thread_b = invoke(graph, "会话B第一问", "thread-b")

    assert human_texts(thread_a) == ["会话A第一问", "会话A第二问"]
    assert human_texts(thread_b) == ["会话B第一问"]


# --------------------------------- 10 / 11 / 12. 其余分支行为不变


def test_knowledge_intent_still_goes_through_rag() -> None:
    """knowledge 仍走 RAG；Tool Calling 链路完全未被触碰。"""

    rag_service = FakeRAGService(
        answer="图书馆开放时间为周一至周五 8:00–22:00。",
        sources=[{"source": "campus_rules.md", "title": "校园规章", "score": 0.9, "snippet": "…"}],
        retrieved=1,
    )
    llm = ScriptedChatModel.scripted(AIMessage(content="本回答不应出现"))
    graph = build_test_graph(intent="knowledge", llm=llm, rag_service=rag_service)

    state = invoke(graph, "图书馆开放时间是怎么规定的？")

    assert rag_service.calls == [("图书馆开放时间是怎么规定的？", 0)]
    assert state["messages"][-1].content == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert state["sources"]
    # 关键隔离断言：RAG 分支没有调用模型，也没有绑定任何工具
    assert llm.calls == 0
    assert llm.bound_tool_names == []
    assert tool_messages(state) == []


def test_chat_intent_keeps_plain_conversation() -> None:
    """chat 分支行为不变：纯对话、不绑定工具、不产生 ToolMessage。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="你好，有什么可以帮你？"))
    graph = build_test_graph(intent="chat", llm=llm)

    state = invoke(graph, "你好")

    assert state["messages"][-1].content == "你好，有什么可以帮你？"
    assert tool_messages(state) == []
    assert llm.bound_tool_names == []
    # chat 节点不注入系统提示，收到什么就发什么
    assert not any(isinstance(m, SystemMessage) for m in llm.received[0])


def test_unknown_intent_keeps_placeholder_behaviour() -> None:
    """unknown 分支仍由占位节点承接，行为与之前一致。"""

    graph = build_test_graph(intent="unknown")

    state = invoke(graph, "嗯……")

    assert "[unknown]" in str(state["messages"][-1].content)


# --------------------------------------------------- 13. 架构边界守卫


def test_tool_calling_layer_never_reaches_rag_internals() -> None:
    """Tool Calling 层禁止依赖向量库 / Embedding / RAG 清单等底层设施。"""

    source = Path(tool_agent_module.__file__).read_text(encoding="utf-8")
    import_lines = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]
    forbidden = (
        "chroma",
        "milvus",
        "faiss",
        "vectorstore",
        "embeddings",
        "manifest",
        "incremental",
        "retriever",
        "rag.",
    )

    assert import_lines, "未扫描到 import 行，守卫用例失效"
    for line in import_lines:
        lowered = line.lower()
        for token in forbidden:
            assert token not in lowered, f"Tool Calling 层出现 RAG 底层依赖: {line.strip()}"


def test_tool_node_uses_official_implementation() -> None:
    """工具执行必须走官方 ToolNode，不自建执行器。"""

    source = Path(graph_module.__file__).read_text(encoding="utf-8")

    assert "from langgraph.prebuilt import ToolNode, tools_condition" in source
    assert "name=TOOLS_NODE" in source
    assert TOOLS_NODE == "tools"  # 与 tools_condition 的取值保持一致


def test_exam_tool_contract_is_stable() -> None:
    """工具返回结构稳定：list[dict]，含 course/date/time/room 四个字段。"""

    rows = get_exam_schedule.invoke({})

    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row) == {"course", "date", "time", "room"}


# ============================================================================
# 多工具 Tool Calling：AGENT_TOOLS = 考试安排 / 课程表 / 成绩
# ============================================================================


# ---------------------------------------------- 1. 三个工具都被绑定给模型


def test_all_registered_tools_are_bound_to_the_model() -> None:
    """模型一次就能看到全部已登记工具。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="好的"))
    graph = build_test_graph(llm=llm)

    invoke(graph, "你好")

    assert llm.bound_tool_names == EXPECTED_TOOL_NAMES


def test_tool_agent_is_the_only_tool_registry() -> None:
    """工具清单只有一处：节点模块定义它，图侧只引用、不重定义。

    守住「不允许出现两份独立工具列表」：若哪天有人在 ``graph.py`` 里另起一份
    工具元组，这里会立刻失败。
    """

    agent_source = Path(tool_agent_module.__file__).read_text(encoding="utf-8")
    graph_source = Path(graph_module.__file__).read_text(encoding="utf-8")

    assert "AGENT_TOOLS: Final" in agent_source
    assert "AGENT_TOOLS" in graph_source
    # 图定义里不允许出现具体工具名（否则等于第二份清单）
    for name in EXPECTED_TOOL_NAMES:
        assert name not in graph_source, f"图定义中出现了具体工具名 {name}，疑似第二份工具清单"


def test_agent_tools_are_query_only_and_distinct() -> None:
    """AGENT_TOOLS 只含三个只读查询工具，无重复登记，也未混入 RAG 能力。"""

    names = tuple(tool_obj.name for tool_obj in AGENT_TOOLS)

    assert names == tuple(EXPECTED_TOOL_NAMES)
    assert len(names) == len(set(names)), "工具清单出现重复登记"
    for tool_obj in AGENT_TOOLS:
        assert not any(
            word in tool_obj.name for word in ("rag", "knowledge", "retriev", "search")
        ), f"RAG / 检索能力不得混入 Tool Calling 工具清单: {tool_obj.name}"

    assert get_exam_schedule in AGENT_TOOLS
    assert get_course_schedule in AGENT_TOOLS
    assert get_score in AGENT_TOOLS


# ------------------------------------ 2 / 3 / 4. 各工具被正确选择并执行


@pytest.mark.parametrize(
    ("name", "args", "expected_tokens", "absent_tokens"),
    [
        # 考试安排：科目 / 日期 / 考场
        ("get_exam_schedule", {}, ("计算机网络", "2026-09-28", "A302"), ()),
        # 课表：week=3 时，第 5 周才开课的「数据库原理」必须被过滤掉
        (
            "get_course_schedule",
            {"week": 3},
            ("数据结构", "周一", "08:00-09:40"),
            ("数据库原理",),
        ),
        # 成绩：按课程过滤，只应命中数据结构
        (
            "get_score",
            {"course": "数据结构"},
            ("2025-2026-1", "92.0"),
            ("大学英语", "高等数学"),
        ),
    ],
)
def test_each_tool_is_selected_and_executed(
    name: str,
    args: dict[str, Any],
    expected_tokens: tuple[str, ...],
    absent_tokens: tuple[str, ...],
) -> None:
    """模型请求哪个工具就执行哪个，且返回值来自工具的真实查询（而非模型编造）。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message(name, args=args),
        AIMessage(content="查询完成。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "查询一下")

    executed = tool_messages(state)
    assert len(executed) == 1
    assert executed[0].name == name
    assert executed[0].status == "success"
    for token in expected_tokens:
        assert token in executed[0].content
    for token in absent_tokens:
        assert token not in executed[0].content
    assert llm.calls == 2  # agent -> tools -> agent


def test_tool_arguments_are_passed_through_verbatim() -> None:
    """模型给出的参数被原样传给工具函数（不只是「工具被调用了」）。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message("echo_args", args={"week": 7, "course": "数据结构"}),
        AIMessage(content="好的"),
    )
    graph = build_test_graph(llm=llm, tools=[echo_args])

    state = invoke(graph, "查询")

    executed = tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "success"
    assert json.loads(executed[0].content) == {"week": 7, "course": "数据结构"}


# --------------------------------- 5. 同一轮对话里连续调用两个工具


def test_agent_can_call_two_tools_in_sequence() -> None:
    """模型可跨轮连续发起两次工具调用：agent -> tools -> agent -> tools -> agent。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message("get_course_schedule", call_id="c1", args={"week": 3}),
        tool_call_message("get_exam_schedule", call_id="c2"),
        AIMessage(content="先把课表给你，考试安排也在下面。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "先看看我的课，再说考试")

    executed = tool_messages(state)
    # 两轮分别执行，顺序即执行顺序（同一批次内才需要按 id 配对）
    assert [m.name for m in executed] == ["get_course_schedule", "get_exam_schedule"]
    assert all(m.status == "success" for m in executed)
    assert llm.calls == 3
    # 第二次模型调用发生在第一次工具执行之后：它必须能看到第一次的工具结果
    second_round = llm.received[1]
    assert any(
        isinstance(m, ToolMessage) and m.name == "get_course_schedule" for m in second_round
    )


# -------------------------------- 6 / 7. 一次请求多个工具：全部执行、数量正确


def test_multiple_tool_calls_in_one_message_are_all_executed() -> None:
    """一条 AIMessage 里的多个 tool_calls 会被 ToolNode 全部执行。"""

    llm = ScriptedChatModel.scripted(
        tool_call_messages(
            ("get_course_schedule", {"week": 3}),
            ("get_exam_schedule", {}),
        ),
        AIMessage(content="课表和考试安排都查好了。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "我这周有什么课，最近还有什么考试？")

    executed = tool_messages(state)
    assert len(executed) == 2
    assert all(m.status == "success" for m in executed)
    # 按 tool_call_id 配对，不依赖批量执行时 ToolMessage 的先后顺序
    by_call_id = {m.tool_call_id: m for m in executed}
    assert set(by_call_id) == {"call-1", "call-2"}
    assert by_call_id["call-1"].name == "get_course_schedule"
    assert by_call_id["call-2"].name == "get_exam_schedule"
    assert llm.calls == 2  # 一个批次即可完成，无需多轮
    # 第二次模型调用同时收到两条工具结果
    assert len([m for m in llm.received[1] if isinstance(m, ToolMessage)]) == 2
    assert state["messages"][-1].content == "课表和考试安排都查好了。"


def test_three_tools_in_one_round_are_all_executed() -> None:
    """三个工具在同一轮被请求时全部执行（覆盖 AGENT_TOOLS 全量）。"""

    llm = ScriptedChatModel.scripted(
        tool_call_messages(
            ("get_exam_schedule", {}),
            ("get_course_schedule", {"week": 3}),
            ("get_score", {"course": "数据结构"}),
        ),
        AIMessage(content="汇总如下。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "课表、考试、数据结构成绩都给我")

    executed = tool_messages(state)
    assert len(executed) == len(AGENT_TOOLS) == 3
    assert sorted(m.name for m in executed) == sorted(EXPECTED_TOOL_NAMES)
    assert all(m.status == "success" for m in executed)
    assert llm.calls == 2


def test_default_graph_can_execute_every_registered_tool() -> None:
    """默认图（不注入 tools）里，ToolNode 拥有的工具与模型看到的完全一致。"""

    llm = ScriptedChatModel.scripted(
        tool_call_messages(*((name, {}) for name in EXPECTED_TOOL_NAMES)),
        AIMessage(content="都查完了。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "全部查一遍")

    executed = tool_messages(state)
    assert len(executed) == len(EXPECTED_TOOL_NAMES)
    assert sorted(m.name for m in executed) == sorted(EXPECTED_TOOL_NAMES)
    assert all(m.status == "success" for m in executed)


def test_final_answer_is_grounded_in_multiple_tool_results() -> None:
    """最终回答建立在**全部**工具结果之上（课表与考试数据都出现）。"""

    llm = EchoToolResultModel(
        first_request=tool_call_messages(
            ("get_course_schedule", {"week": 3}),
            ("get_exam_schedule", {}),
        )
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "我这周有什么课，最近还有什么考试？")

    final = str(state["messages"][-1].content)
    assert final.startswith("查询结果：")
    # 课表数据与考试数据都必须出现在最终回答里
    for token in ("数据结构", "周一", "计算机网络", "2026-09-28", "A302"):
        assert token in final
    assert llm.calls == 2


# ------------------------------------------------- 8 / 9. 异常与参数校验


def test_tool_exception_does_not_break_other_tool_calls_in_same_batch() -> None:
    """同一批次里一个工具抛异常，不影响其它工具执行，也不会打崩整张图。"""

    llm = ScriptedChatModel.scripted(
        tool_call_messages(
            ("broken_tool", {}),
            ("get_exam_schedule", {}),
        ),
        AIMessage(content="考试安排查到了，另一项暂时失败。"),
    )
    graph = build_test_graph(llm=llm, tools=[broken_tool, get_exam_schedule])

    state = invoke(graph, EXAM_QUESTION)

    executed = tool_messages(state)
    assert len(executed) == 2
    by_call_id = {m.tool_call_id: m for m in executed}
    failed = by_call_id["call-1"]
    assert failed.status == "error"
    assert "工具执行失败" in failed.content
    assert "RuntimeError" in failed.content
    # 失败被隔离：另一个工具照常成功返回
    assert by_call_id["call-2"].status == "success"
    assert "计算机网络" in by_call_id["call-2"].content
    assert state["messages"][-1].content == "考试安排查到了，另一项暂时失败。"


def _refused_client():
    """构造「业务服务未启动」的替身客户端。"""

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    return make_campus_client(refused)


def test_exam_tool_service_failure_does_not_break_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """考试工具背后的业务服务不可用时：整轮不中断，模型拿到的是人话。

    本阶段最贴近生产的一条用例：``get_exam_schedule`` 现在会真的发 HTTP，
    下游（Spring Boot / MySQL）随时可能不可达。要守住两点——
    图不能崩，且错误必须是可读文案而不是 traceback。
    """

    patch_campus_api(monkeypatch, _refused_client())

    llm = ScriptedChatModel.scripted(
        tool_call_message("get_exam_schedule"),
        AIMessage(content="校园业务服务暂时不可用，请稍后再试。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, EXAM_QUESTION)

    executed = tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "校园业务服务暂时不可用" in executed[0].content
    assert "Traceback" not in executed[0].content
    assert "http://" not in executed[0].content
    # 图未中断：模型仍然完成了一次后续调用并给出最终回答
    assert llm.calls == 2
    assert state["messages"][-1].content == "校园业务服务暂时不可用，请稍后再试。"


@pytest.mark.parametrize("status_code", [404, 503])
def test_exam_tool_http_error_is_isolated_from_other_tools(
    status_code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4xx / 5xx 被隔离成该次调用自己的 error 消息，不影响同批次其它工具。"""

    patch_campus_api(monkeypatch, make_campus_client(status_code=status_code))

    llm = ScriptedChatModel.scripted(
        tool_call_messages(
            ("get_exam_schedule", {}),
            ("get_score", {"course": "数据结构"}),
        ),
        AIMessage(content="成绩查到了，考试安排暂时查不到。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, "考试和数据结构成绩都给我")

    by_call_id = {m.tool_call_id: m for m in tool_messages(state)}
    assert by_call_id["call-1"].status == "error"  # 考试：下游服务返回错误
    assert str(status_code) in by_call_id["call-1"].content
    assert by_call_id["call-2"].status == "success"  # 成绩：由替身服务正常返回
    assert state["messages"][-1].content == "成绩查到了，考试安排暂时查不到。"


def test_invalid_tool_argument_becomes_error_message() -> None:
    """真实工具的取值校验失败时，错误原因作为 error 消息回流给模型。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message("get_course_schedule", args={"week": 99}),
        AIMessage(content="周次超出范围，请确认后重试。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, COURSE_QUESTION)

    executed = tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "工具执行失败（ValueError）" in executed[0].content
    assert "1-30" in executed[0].content
    assert state["messages"][-1].content == "周次超出范围，请确认后重试。"


def test_model_can_recover_from_tool_argument_error() -> None:
    """模型可据错误消息自行改参重试（ReAct 循环的自愈能力）。"""

    llm = ScriptedChatModel.scripted(
        # 第 1 次：周次越界 -> 工具本体的取值校验失败，错误消息里带上可选范围
        tool_call_message("get_course_schedule", call_id="c1", args={"week": 99}),
        # 第 2 次：据错误消息改参，换成成绩查询并成功
        tool_call_message("get_score", call_id="c2", args={"course": "数据结构"}),
        AIMessage(content="数据结构 92 分。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, SCORE_QUESTION)

    executed = tool_messages(state)
    assert [m.status for m in executed] == ["error", "success"]
    # 错误消息里带上可选范围，模型据此改参
    assert "1-30" in executed[0].content
    assert '"score": 92.0' in executed[1].content
    assert state["messages"][-1].content == "数据结构 92 分。"


def test_unknown_tool_does_not_block_known_tool_in_same_batch() -> None:
    """模型请求不存在的工具时只影响该次调用，其它工具照常执行。"""

    llm = ScriptedChatModel.scripted(
        tool_call_messages(
            ("no_such_tool", {}),
            ("get_score", {"course": "数据结构"}),
        ),
        AIMessage(content="成绩已查到。"),
    )
    graph = build_test_graph(llm=llm)

    state = invoke(graph, SCORE_QUESTION)

    executed = tool_messages(state)
    assert len(executed) == 2
    by_call_id = {m.tool_call_id: m for m in executed}
    unknown = by_call_id["call-1"]
    assert unknown.status == "error"
    assert "no_such_tool" in unknown.content
    assert "not a valid tool" in unknown.content
    assert by_call_id["call-2"].status == "success"
    assert '"score": 92.0' in by_call_id["call-2"].content


# ------------------------------------------------ 10 / 11. 多轮与会话隔离


def test_multi_turn_history_keeps_tool_results_of_each_tool() -> None:
    """多轮会话里，两轮各自调用的工具结果都保留在消息历史中。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message("get_course_schedule", call_id="c1", args={"week": 3}),
        AIMessage(content="这周有 6 节课。"),
        tool_call_message("get_exam_schedule", call_id="c2"),
        AIMessage(content="还有两门考试。"),
    )
    graph = build_test_graph(llm=llm, checkpointer=InMemorySaver())

    first = invoke(graph, COURSE_QUESTION, "thread-a")
    assert [m.name for m in tool_messages(first)] == ["get_course_schedule"]

    second = invoke(graph, "那最近有什么考试？", "thread-a")

    assert human_texts(second) == [COURSE_QUESTION, "那最近有什么考试？"]
    # 第二轮状态里累积了两轮的工具结果（checkpointer 未被破坏）
    assert [m.name for m in tool_messages(second)] == [
        "get_course_schedule",
        "get_exam_schedule",
    ]
    assert llm.calls == 4
    # 第二轮模型能看到第一轮的工具结果
    assert any(
        isinstance(m, ToolMessage) and m.name == "get_course_schedule"
        for m in llm.received[2]
    )
    assert second["messages"][-1].content == "还有两门考试。"


def test_thread_id_isolation_holds_with_multiple_tools() -> None:
    """不同 thread_id 的工具结果互不可见。"""

    llm = ScriptedChatModel.scripted(
        tool_call_message("get_course_schedule", call_id="c1", args={"week": 3}),
        AIMessage(content="会话A：这周有 6 节课。"),
        tool_call_message("get_exam_schedule", call_id="c2"),
        AIMessage(content="会话B：两门考试。"),
    )
    graph = build_test_graph(llm=llm, checkpointer=InMemorySaver())

    thread_a = invoke(graph, COURSE_QUESTION, "thread-a")
    thread_b = invoke(graph, EXAM_QUESTION, "thread-b")

    assert [m.name for m in tool_messages(thread_a)] == ["get_course_schedule"]
    assert [m.name for m in tool_messages(thread_b)] == ["get_exam_schedule"]
    assert thread_a["messages"][-1].content == "会话A：这周有 6 节课。"
    assert thread_b["messages"][-1].content == "会话B：两门考试。"
    assert human_texts(thread_b) == [EXAM_QUESTION]


# ------------------------------------------------- 新增工具自身的契约补充


def test_course_tool_contract_is_stable() -> None:
    """课表工具返回 list[dict]，含 6 个字段且取值均为字符串。"""

    rows = get_course_schedule.invoke({})

    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row) == {"course", "teacher", "weekday", "time", "room", "weeks"}
        assert all(isinstance(value, str) for value in row.values())


def test_course_tool_filters_by_week() -> None:
    """week 参数真实生效：第 3 周排除第 5 周才开课的课程。"""

    all_rows = get_course_schedule.invoke({})
    week3 = get_course_schedule.invoke({"week": 3})
    week8 = get_course_schedule.invoke({"week": 8})

    assert "数据库原理" not in {row["course"] for row in week3}
    assert "数据库原理" in {row["course"] for row in week8}
    assert len(week3) < len(all_rows)
    assert len(week8) == len(all_rows)


def test_score_tool_contract_is_stable() -> None:
    """成绩工具返回 list[dict]；分数与学分保持数值类型，便于模型直接比较。"""

    rows = get_score.invoke({})

    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row) == {"course", "semester", "credit", "score"}
        assert isinstance(row["score"], float)
        assert isinstance(row["credit"], float)
        assert 0.0 <= row["score"] <= 100.0


def test_score_tool_filters_by_course_and_semester() -> None:
    """semester（服务端过滤）与 course（本地挑选）都真实生效。"""

    by_course = get_score.invoke({"course": "数据结构"})
    assert [row["course"] for row in by_course] == ["数据结构"]

    by_semester = get_score.invoke({"semester": "2024-2025-2"})
    assert {row["course"] for row in by_semester} == {"高等数学", "大学英语"}

    # 双向包含匹配：模型补全成「数据结构课程」也能命中
    assert len(get_score.invoke({"course": "数据结构课程"})) == 1
    # 未提供课程 / 查不到时返回空列表，而不是报错
    assert get_score.invoke({"course": "量子力学"}) == []


def test_tool_parameter_validation_rejects_bad_values() -> None:
    """工具自身的参数校验：非法周次与类型不符的文本参数都被拒绝。

    成绩工具**不再**用写死的学期清单做校验：学期现在由服务端按数据库精确匹配
    （查不到的学期返回空列表，不是错误），「哪些学期存在」这件事只应有一处来源。
    """

    with pytest.raises(ValueError):
        get_course_schedule.invoke({"week": 99})

    # 直接调用函数本体时类型校验同样生效（工具调用路径由 args_schema 先行把住）
    with pytest.raises(TypeError):
        get_course_schedule.func(week=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        get_score.func(semester=2025)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        get_score.func(course=2025)  # type: ignore[arg-type]
