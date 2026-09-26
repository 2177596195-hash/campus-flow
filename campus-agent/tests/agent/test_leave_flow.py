"""请假申请的多轮状态测试（完全离线，不触网、不调真实模型）。

本文件把「请假」这条**写操作**链路拆成五组边界来守：

1. **缺参数就追问**：``action`` 意图 + 「我要请假」这类信息明显不全的说法 ->
   ``task_slot`` 写入待办（``pending_slot = leave_detail``）并**确定性地**追问一句，
   本轮结束，一次模型调用都不花；
2. **说全了就放行**：起止时间与事由都在句子里时不拦截，直接走 ``agent``，
   行为与没有这套槽位时一致；
3. **补齐后恢复任务**：第二轮只补一句「10月12日到10月14日，感冒发烧」——
   孤立识别很可能判成 ``unknown``，但待办把它解释成「给请假任务补参数」，
   于是 ``intent`` 恢复成 ``action``，模型收到一句任务上下文后自己产出
   ``submit_leave_application`` 的 ``tool_calls``，由官方 ``ToolNode`` 执行；
4. **补充信息不会被误判成换话题**：请假槽位接受任意非空文本，
   因此「用户随口补一句」永远不会触发待办取消（这条与学期槽位刻意不同）；
5. **不回归与纯函数边界**：学期槽位的路径一个字节没变；请假判定是可离线断言的
   纯函数；``encode`` / ``decode`` 往返自洽。

核心手法与 ``test_task_state.py`` 一致：``ScriptedChatModel`` 确定性地产出
``AIMessage(tool_calls=[...])``，``ScriptedIntentClassifier`` 逐轮给出孤立识别结果，
``InMemorySaver`` 提供真实 checkpointer；工具侧用 ``FakeMCPClient`` +
真实适配层（请假只在 MCP 上暴露），因此**一个子进程都不需要起**。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END

from app.agent.graph.graph import route_after_task_slot
from app.agent.intent import INTENTS
from app.agent.slots import (
    LEAVE_ACTION_INTENT,
    SLOT_LEAVE,
    SLOT_SEMESTER,
    decode_leave_detail,
    encode_leave_detail,
    missing_slot,
    slot_hint,
    slot_question,
)
from app.mcp.tools import LEAVE_TOOL_NAME, build_agent_tools
from tests._fakes import (
    LEAVE_APPLICATION_PAYLOAD,
    FakeMCPClient,
    ScriptedChatModel,
    ScriptedIntentClassifier,
    StaticIntentClassifier,
    make_mcp_server_tool_definitions,
    make_mcp_settings,
)

#: 信息明显不全的请假请求（演示场景里的第一句话）
BARE_LEAVE_REQUEST = "我要请假"
#: 第二轮补充信息：只有口语化的时间与事由，单独看什么都不是
LEAVE_SUPPLEMENT = "10月12日早上8点到10月14日下午6点，感冒发烧"

#: 任务上下文提示里的标志串
HINT_MARKER = "【任务上下文】"

THREAD_A = "thread-a"

# --------------------------------------------------------------- 测试用构造


def tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def build_graph_with(
    *,
    classifier: Any,
    llm: ScriptedChatModel,
    tools: Any = None,
    with_checkpointer: bool = True,
):
    """装配被测图：模型 / 意图识别器全部为替身，checkpointer 为真实实现。"""

    from app.agent.graph import build_graph
    from tests._fakes import FakeRAGService

    return build_graph(
        llm=llm,
        intent_classifier=classifier,
        rag_service=FakeRAGService(),  # type: ignore[arg-type]
        checkpointer=InMemorySaver() if with_checkpointer else None,
        tools=tools,
    )


def mcp_leave_tools(client: FakeMCPClient):
    """按 MCP 开启的方式解析工具清单（三个查询槽位 + 追加的请假槽位）。"""

    return build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)


def leave_client(**payload_overrides: Any) -> FakeMCPClient:
    """替身 MCP 客户端：注册四个工具，并给出请假提交的返回值。"""

    payloads = {LEAVE_TOOL_NAME: LEAVE_APPLICATION_PAYLOAD}
    payloads.update(payload_overrides)
    return FakeMCPClient(tools=make_mcp_server_tool_definitions(), payloads=payloads)


def invoke(graph: Any, text: str, thread_id: str = THREAD_A) -> dict[str, Any]:
    return graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def reply_of(state: dict[str, Any]) -> str:
    return str(state["messages"][-1].content)


def tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [message for message in state["messages"] if isinstance(message, ToolMessage)]


def first_prompt(llm: ScriptedChatModel, index: int = 0) -> str:
    """第 ``index`` 次模型调用收到的**系统提示**（一定是消息列表的第一条）。"""

    return str(llm.received[index][0].content)


def has_persisted_system_prompt(state: dict[str, Any]) -> bool:
    return any(isinstance(message, SystemMessage) for message in state["messages"])


# ============================================================================
# 1. 缺参数：写入待办 + 确定性追问（不花模型调用）
# ============================================================================


def test_bare_leave_request_asks_back_without_calling_the_model() -> None:
    """「我要请假」信息不全 -> 确定性追问一句，本轮结束，模型一次都没调。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="这条回复不该被用到"))
    graph = build_graph_with(classifier=StaticIntentClassifier("action"), llm=llm)

    state = invoke(graph, BARE_LEAVE_REQUEST)

    assert reply_of(state) == slot_question(SLOT_LEAVE)
    assert state["pending_intent"] == LEAVE_ACTION_INTENT
    assert state["pending_slot"] == SLOT_LEAVE
    assert state["pending_value"] is None
    assert llm.calls == 0
    assert tool_messages(state) == []
    assert route_after_task_slot(state) == END
    # 追问轮次不写系统提示进状态（否则会在 checkpointer 里逐轮堆积）
    assert not has_persisted_system_prompt(state)


def test_asking_back_for_leave_does_not_expose_tools() -> None:
    """追问发生在 ``agent`` 之前，因此本轮连工具绑定都没发生。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="不该被用到"))
    graph = build_graph_with(classifier=StaticIntentClassifier("action"), llm=llm)

    invoke(graph, BARE_LEAVE_REQUEST)

    assert llm.bound_tool_names == []


# ============================================================================
# 2. 说全了就放行（不回归）
# ============================================================================


@pytest.mark.parametrize(
    "text",
    [
        "我要请假，10月12日到10月14日，感冒发烧",
        "帮我提交请假申请，2026-10-12 08:00 到 2026-10-14 18:00，家中有事",
        "我要请假，2026-10-12 08:00-2026-10-12 18:00，去医院复诊",
    ],
)
def test_complete_leave_request_is_not_intercepted(text: str) -> None:
    """起止时间与事由都在句子里时不追问，直接进入 ``agent`` 分支。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="好的"))
    graph = build_graph_with(classifier=StaticIntentClassifier("action"), llm=llm)

    state = invoke(graph, text)

    assert state.get("pending_slot") is None
    assert state.get("pending_intent") is None
    assert state.get("pending_value") is None
    assert reply_of(state) == "好的"
    assert llm.calls == 1


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        # 不含请假触发词
        ("帮我提交退课申请", "action"),
        ("我要请客", "action"),
        ("我的请假批了吗", "chat"),
        ("请假流程是怎样的", "knowledge"),
        # 不是 action 意图：一律不追问
        ("我要请假", "chat"),
        ("我要请假", "knowledge"),
        ("我要请假", "query"),
        ("我要请假", None),
    ],
)
def test_leave_slot_only_triggers_for_action_requests_that_need_details(
    text: str, intent: object
) -> None:
    """只有「action + 提到请假 + 信息不全」三者同时成立才追问。"""

    assert missing_slot(text, intent) is None


def test_leave_and_semester_slots_do_not_cross_trigger() -> None:
    """两个槽位的判据互不干扰（同一句话不会同时命中两个规则）。"""

    assert missing_slot("帮我查一下成绩", "action") is None
    assert missing_slot(BARE_LEAVE_REQUEST, "query") is None


# ============================================================================
# 3. 补齐 -> 恢复任务 -> 走完整 MCP 写链路
# ============================================================================


def test_filling_the_leave_slot_restores_the_task_and_submits_through_mcp() -> None:
    """第二轮只补一句时间与事由：靠待办恢复成请假任务，再由 Agent 提交申请。"""

    client = leave_client()
    tools = mcp_leave_tools(client)
    llm = ScriptedChatModel.scripted(
        tool_call(
            LEAVE_TOOL_NAME,
            {
                "start_time": "2026-10-12 08:00",
                "end_time": "2026-10-14 18:00",
                "reason": "感冒发烧",
            },
        ),
        AIMessage(content="请假申请已提交，申请编号为 1，当前状态为待审核。"),
    )
    # 第二轮那句补充信息单独看什么都不是（孤立识别给 unknown）——正是要解决的问题
    classifier = ScriptedIntentClassifier(LEAVE_ACTION_INTENT, "unknown")
    graph = build_graph_with(classifier=classifier, llm=llm, tools=tools)

    first = invoke(graph, BARE_LEAVE_REQUEST)
    assert first["pending_slot"] == SLOT_LEAVE

    second = invoke(graph, LEAVE_SUPPLEMENT)

    # (1) 状态被恢复：意图取自待办，而不是本轮那个 unknown
    assert classifier.seen == [BARE_LEAVE_REQUEST, LEAVE_SUPPLEMENT]
    assert second["intent"] == LEAVE_ACTION_INTENT
    # (2) 槽位被消费掉
    assert second["pending_slot"] is None
    assert second["pending_value"] is None
    # (3) 写操作真的执行了：走的是 MCP 适配器 -> 客户端
    executed = tool_messages(second)
    assert [message.name for message in executed] == [LEAVE_TOOL_NAME]
    assert executed[0].status == "success"
    assert "PENDING" in executed[0].content
    # (4) 学号由宿主绑定补齐，三个参数来自模型
    assert client.calls == [
        (
            LEAVE_TOOL_NAME,
            {
                "student_id": "student-001",
                "start_time": "2026-10-12 08:00",
                "end_time": "2026-10-14 18:00",
                "reason": "感冒发烧",
            },
        )
    ]
    # (5) 最终回答建立在提交结果之上
    assert reply_of(second) == "请假申请已提交，申请编号为 1，当前状态为待审核。"
    # (6) 上下文只随请求发出，不写回状态
    assert not has_persisted_system_prompt(second)
    # (7) 停在一个**非空**待办上不会让图结束（条件边按补齐后的意图路由）
    assert route_after_task_slot(second) == "agent"


def test_task_context_tells_the_model_which_tool_to_use() -> None:
    """补齐当轮注入的任务上下文必须点名工具与字段，且只注入一次。"""

    client = leave_client()
    tools = mcp_leave_tools(client)
    llm = ScriptedChatModel.scripted(
        tool_call(
            LEAVE_TOOL_NAME,
            {"start_time": "2026-10-12 08:00", "end_time": "2026-10-12 18:00", "reason": "事由"},
        ),
        AIMessage(content="已提交。"),
        AIMessage(content="不客气。"),
    )
    classifier = ScriptedIntentClassifier(LEAVE_ACTION_INTENT, "unknown", "chat")
    graph = build_graph_with(classifier=classifier, llm=llm, tools=tools)

    invoke(graph, BARE_LEAVE_REQUEST)
    invoke(graph, LEAVE_SUPPLEMENT)
    third = invoke(graph, "谢谢")

    hint = first_prompt(llm, index=0)
    assert HINT_MARKER in hint
    assert LEAVE_TOOL_NAME in hint
    assert LEAVE_SUPPLEMENT in hint  # 用户原话被原样带给模型
    for field in ("start_time", "end_time", "reason"):
        assert field in hint
    # 工具回边后的第二次调用已拿到工具结果，不再需要上下文
    assert HINT_MARKER not in first_prompt(llm, index=1)
    # 下一轮更不该再出现
    assert HINT_MARKER not in first_prompt(llm, index=2)
    assert third["pending_value"] is None
    assert third["pending_slot"] is None


def test_slot_hint_is_absent_without_a_value() -> None:
    """没有待使用的值时提示为 ``None``：调用方据此完全不改动请求。"""

    assert slot_hint(None) is None
    assert slot_hint("") is None


# ============================================================================
# 4. 补充信息不会被误判成「换话题」
# ============================================================================


def test_leave_supplement_is_consumed_even_when_isolated_intent_says_chat() -> None:
    """请假槽位接受任意非空文本，因此补充信息**永远不会**触发待办取消。

    这条与学期槽位刻意不同：学期解析不出值时会走「被新任务取代」的判断
    （``SELF_CONTAINED_INTENTS``），而请假的补充信息常常是一句口语
    （「嗯……就是那个」），若被当成「用户换了话题」就会把参数丢掉。
    """

    llm = ScriptedChatModel.scripted(AIMessage(content="好的"))
    classifier = ScriptedIntentClassifier(LEAVE_ACTION_INTENT, "chat")
    graph = build_graph_with(classifier=classifier, llm=llm)

    invoke(graph, BARE_LEAVE_REQUEST)
    second = invoke(graph, "嗯……就是那个")

    # 没有被取消：意图恢复成 action，待办清空，补充信息被 agent 节点消费
    assert second["intent"] == LEAVE_ACTION_INTENT
    assert second["pending_slot"] is None
    assert second["pending_value"] is None
    # 也没有再追问一句
    assert reply_of(second) == "好的"


def test_unresolved_semester_slot_still_cancels_on_a_new_task() -> None:
    """反向守卫：学期槽位的取消语义没有被请假的改动带坏。"""

    from app.agent.slots import SCORE_QUERY_INTENT

    llm = ScriptedChatModel.scripted(AIMessage(content="你好呀"))
    classifier = ScriptedIntentClassifier(SCORE_QUERY_INTENT, "chat")
    graph = build_graph_with(classifier=classifier, llm=llm)

    invoke(graph, "帮我查一下成绩")
    moved = invoke(graph, "你好")

    assert moved.get("pending_slot") is None
    assert moved.get("pending_intent") is None
    assert reply_of(moved) == "你好呀"


# ============================================================================
# 5. 纯函数边界与契约一致性
# ============================================================================


def test_leave_action_intent_matches_the_intent_contract() -> None:
    """槽位模块里的意图字面量必须仍在意图契约里，免得悄悄漂移。"""

    assert LEAVE_ACTION_INTENT in INTENTS


def test_encode_decode_leave_detail_round_trips() -> None:
    """``pending_value`` 的请假值自描述：编码后能解回原文，且不污染学期值。"""

    encoded = encode_leave_detail(LEAVE_SUPPLEMENT)

    assert encoded is not None
    assert encoded.startswith(f"{SLOT_LEAVE}:")
    assert decode_leave_detail(encoded) == LEAVE_SUPPLEMENT

    # 学期值（裸字符串）不会被误判成请假值
    assert decode_leave_detail("2024-2025-2") is None
    assert decode_leave_detail(None) is None
    assert decode_leave_detail(123) is None


@pytest.mark.parametrize("blank", ["", "   ", "\n", None])
def test_encode_leave_detail_rejects_blank_input(blank: object) -> None:
    """没有可记录的内容时返回 ``None``，调用方据此继续追问。"""

    assert encode_leave_detail(blank) is None  # type: ignore[arg-type]


def test_semester_slot_hint_is_unchanged() -> None:
    """学期槽位的上下文渲染保持原样（两处提示互不串台）。"""

    hint = slot_hint("2024-2025-2")

    assert hint is not None
    assert HINT_MARKER in hint
    assert "get_score" in hint
    assert "2024-2025-2" in hint
    # 学期值的提示不该提到写工具
    assert LEAVE_TOOL_NAME not in hint


def test_leave_slot_question_mentions_what_is_missing() -> None:
    """追问文案要说清「缺哪两样」，否则用户不知道该怎么补。"""

    question = slot_question(SLOT_LEAVE)

    assert "时间" in question
    assert "事由" in question
    assert slot_question(SLOT_SEMESTER) != question


def test_slots_module_stays_a_pure_function_module() -> None:
    """守卫：请假判定同样是纯函数——slots 模块不得引入运行期依赖。"""

    from pathlib import Path

    from app.agent import slots as slots_module

    source = Path(slots_module.__file__).read_text(encoding="utf-8")
    module_imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]

    for line in module_imports:
        for token in ("langchain", "langgraph", "pydantic", "httpx", "app."):
            assert token not in line, f"slots.py 顶层出现依赖: {line.strip()}"
    # 不做任何模型调用
    assert ".invoke(" not in source
