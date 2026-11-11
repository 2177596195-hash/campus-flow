"""LangGraph 多轮任务状态测试（完全离线，不触网、不调真实模型）。

本文件覆盖的策略只有一条，但覆盖到底：**成绩查询缺学期**。

    START -> intent_router -> task_slot -> (条件路由) -> ...

- 第一轮「帮我查一下成绩」：``query`` 意图但没有学期 -> ``task_slot`` 写入待办
  （``pending_intent`` / ``pending_slot``）并**确定性地**追问一句，本轮结束，
  一次模型调用都不花。
- 第二轮「上学期」：单看这句话什么都不是（孤立识别会判成 unknown），但待办里
  记着「上一轮在查成绩、缺学期」，于是本轮被解释成「补一个学期」——
  槽位被解析成具体学期串，``intent`` 恢复成待办任务的意图，模型收到一句任务
  上下文后**自己**产出 ``tool_calls``，工具仍由官方 ``ToolNode`` 执行。
- 另一个 ``thread_id`` 看不到这条待办，因此它的「上学期」不会被当成补槽位。

核心手法：``ScriptedChatModel`` 确定性地产出 ``AIMessage(tool_calls=[...])``，
``ScriptedIntentClassifier`` 逐轮给出孤立识别结果，``InMemorySaver`` 提供真实
checkpointer。因此「状态是否被保存 / 是否被恢复 / 是否被隔离」全部可断言，
不需要真实 LLM。

本文件同时守住九组边界（与阶段要求一一对应）：

1. ``AgentState`` 新增字段与意图契约一致；
2. 学期解析（口语 / 显式串 / 非法串）；
3. 缺槽位识别与追问（不花模型调用、不执行工具）；
4. 已带范围的问题不被拦截（不回归）；
5. 槽位补齐 -> 意图恢复 -> 走完整 Tool Calling 链路取数；
6. checkpointer 同 thread 恢复、跨 thread 隔离；
7. 待办的生命周期（补不上继续问、被新任务取代时取消）；
8. 既有链路（单轮成绩 / 课程 / 考试 / chat / RAG）不受影响；
9. 架构守卫（拓扑接线 + 层依赖 + 节点不碰模型/工具/网络）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END

from app.agent import slots as slots_module
from app.agent.graph import graph as graph_module
from app.agent.graph.graph import (
    AGENT_NODE,
    CHAT_NODE,
    INTENT_ROUTER_NODE,
    KNOWLEDGE_NODE,
    ROUTE_TARGETS,
    SLOT_ROUTE_TARGETS,
    TASK_SLOT_NODE,
    UNKNOWN_NODE,
    route_after_task_slot,
)
from app.agent.graph.state import AgentState
from app.agent.intent import INTENTS
from app.agent.nodes import task_slot as task_slot_module
from app.agent.nodes import tool_agent as tool_agent_module
from app.agent.nodes.tool_agent import TOOL_AGENT_SYSTEM_PROMPT
from app.agent.slots import (
    SCORE_QUERY_INTENT,
    SLOT_SEMESTER,
    current_semester,
    missing_slot,
    parse_semester,
    previous_semester,
    resolve_semester,
    slot_hint,
    slot_question,
)
from tests._fakes import (
    FakeRAGService,
    ScriptedChatModel,
    ScriptedIntentClassifier,
    StaticIntentClassifier,
)

#: 本文件把教务日历**钉在替身数据覆盖的学期**（见 ``pin_stub_calendar`` 夹具）：
#: 默认替身成绩数据覆盖 2025-2026-1 与 2024-2025-2 两个学期，
#: 「上学期」按钉住的当前学期推导即 2024-2025-2。真实配置默认值的变化
#: （学期切换）不会影响这些流程用例；配置层本身的默认值由
#: ``tests/tools/test_calendar_source.py`` 单独锁定。
CURRENT = "2025-2026-1"
#: 钉住当前学期的上一个学期。
PREVIOUS = "2024-2025-2"

#: 演示场景里的两句话
BARE_SCORE_QUESTION = "帮我查一下成绩"
SEMESTER_REPLY = "上学期"

#: 任务上下文提示里的标志串（用于断言「注入了几次」）
HINT_MARKER = "【任务上下文】"

THREAD_A = "thread-a"
THREAD_B = "thread-b"


@pytest.fixture(autouse=True)
def pin_stub_calendar(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把教务日历钉在替身数据覆盖的学期（本文件全部用例自动生效）。

    多轮流程里「上学期」由 ``task_slot`` 按**真实配置**推导当前学期再取上一个；
    替身成绩数据只覆盖 ``2025-2026-1`` / ``2024-2025-2`` 两个学期。不钉住的话，
    配置默认值一变（学期切换），这些用例就会因查不到替身数据而变红——
    那是数据没对上，不是被测行为坏了。
    """

    from app.config.settings import get_campus_calendar_settings

    monkeypatch.setenv("CAMPUS_CURRENT_SEMESTER", CURRENT)
    get_campus_calendar_settings.cache_clear()
    yield
    get_campus_calendar_settings.cache_clear()


# --------------------------------------------------------------- 测试用构造


def tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    """构造一条「请求调用工具」的 AI 消息（原生 ``tool_calls`` 结构）。"""

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def build_task_graph(
    *,
    classifier: Any,
    llm: ScriptedChatModel,
    rag_service: FakeRAGService | None = None,
):
    """装配被测图：模型 / 意图识别器 / RAG 全部为替身，checkpointer 为真实实现。"""

    from app.agent.graph import build_graph

    return build_graph(
        llm=llm,
        intent_classifier=classifier,
        rag_service=rag_service or FakeRAGService(),  # type: ignore[arg-type]
        checkpointer=InMemorySaver(),
    )


def invoke(graph: Any, text: str, thread_id: str = THREAD_A) -> dict[str, Any]:
    return graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def reply_of(state: dict[str, Any]) -> str:
    return str(state["messages"][-1].content)


def tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [message for message in state["messages"] if isinstance(message, ToolMessage)]


def human_texts(state: dict[str, Any]) -> list[str]:
    return [
        str(message.content)
        for message in state["messages"]
        if isinstance(message, HumanMessage)
    ]


def first_prompt(llm: ScriptedChatModel, index: int = 0) -> str:
    """第 ``index`` 次模型调用收到的**系统提示**（一定是消息列表的第一条）。"""

    return str(llm.received[index][0].content)


def has_persisted_system_prompt(state: dict[str, Any]) -> bool:
    return any(isinstance(message, SystemMessage) for message in state["messages"])


# ============================================================================
# 1. State 契约
# ============================================================================


def test_state_declares_the_task_context_fields() -> None:
    """``AgentState`` 增加了三个多轮任务上下文槽位，工具调用字段一个没动。"""

    fields = set(AgentState.__annotations__)
    assert {"pending_intent", "pending_slot", "pending_value"} <= fields
    # 工具调用的表达方式没有被改写（仍是原生 messages）
    assert "messages" in fields


def test_score_query_intent_matches_the_intent_contract() -> None:
    """任务槽位模块里的意图字面量必须仍在意图契约里，免得悄悄漂移。"""

    assert SCORE_QUERY_INTENT in INTENTS


def test_current_semester_has_a_single_source() -> None:
    """「当前学期」只有一个生产来源：配置层的教务日历配置。"""

    from app.config.settings import get_campus_calendar_settings

    assert current_semester() == get_campus_calendar_settings().current_semester == CURRENT


# ============================================================================
# 2. 学期解析（纯函数）
# ============================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("上学期", PREVIOUS),
        ("查一下上一学期", PREVIOUS),
        ("本学期", CURRENT),
        ("这学期的成绩出来了吗", CURRENT),
        ("2024-2025-2", "2024-2025-2"),
        ("帮我看看 2023-2024-1 的成绩", "2023-2024-1"),
        # 中文写法同样要能归一化（Agent 追问学期时用户最自然的说法）
        ("2025-2026第一学期", "2025-2026-1"),
        ("查询2026-2027第一学期成绩", "2026-2027-1"),
        ("2025至2026学年第二学期", "2025-2026-2"),
        ("2025-2026 第 1 学期", "2025-2026-1"),
        # 判不出来的情况一律返回 None —— 宁可追问，也不猜一个错学期
        ("不知道", None),
        ("2025-2030-1", None),  # 学年不连续
        ("2025-2030第一学期", None),  # 中文写法同样要求学年连续
        ("大二第一学期", None),  # 没有年份 -> 不猜
        ("2025-2026-12", None),  # 学期序号非法
        ("", None),
    ],
)
def test_resolve_semester(text: str, expected: str | None) -> None:
    assert resolve_semester(text, current=CURRENT) == expected


def test_previous_semester_only_shifts_the_academic_year_when_needed() -> None:
    assert previous_semester("2025-2026-1") == "2024-2025-2"
    assert previous_semester("2025-2026-2") == "2025-2026-1"
    assert previous_semester("2024-2025-2") == "2024-2025-1"
    assert previous_semester("乱写") is None
    assert previous_semester("2025-2030-1") is None


def test_parse_semester_rejects_malformed_shapes() -> None:
    assert parse_semester("2025-2026-1") == "2025-2026-1"
    assert parse_semester("没有学期") is None
    assert parse_semester("2025-2026-12") is None
    assert parse_semester("2025-2030-1") is None


def test_slot_hint_is_absent_without_a_value() -> None:
    """没有待使用的值时提示为 ``None``：调用方据此完全不改动请求。"""

    assert slot_hint(None) is None
    assert slot_hint("") is None

    hint = slot_hint(PREVIOUS)
    assert hint is not None
    assert HINT_MARKER in hint
    assert PREVIOUS in hint
    assert "get_score" in hint


@pytest.mark.parametrize(
    ("text", "intent", "expected"),
    [
        # 触发：问成绩 / 分数，且完全没给学期
        ("帮我查一下成绩", "query", SLOT_SEMESTER),
        ("我想看下分数", "query", SLOT_SEMESTER),
        # 不触发：已经带了课程范围，本身就能作答
        ("数据结构考了多少分？", "query", None),
        # 不触发：已经给了学期
        ("我上学期成绩如何", "query", None),
        ("这学期成绩怎么样", "query", None),
        # 不触发：根本不是成绩问题
        ("图书馆几点开门", "query", None),
        # 不触发：一句话里还牵涉别的教务数据 —— 拦下来会把那几项一起丢掉
        ("课表、考试、数据结构成绩都给我", "query", None),
        ("考试和数据结构成绩都给我", "query", None),
        ("帮我看看课表和成绩", "query", None),
        # 不触发：不是 query 意图
        ("帮我查一下成绩", "chat", None),
        ("帮我查一下成绩", "action", None),
        ("帮我查一下成绩", "knowledge", None),
        ("帮我查一下成绩", None, None),
    ],
)
def test_missing_slot_only_triggers_for_an_unscoped_score_query(
    text: str, intent: object, expected: str | None
) -> None:
    assert missing_slot(text, intent) == expected


# ============================================================================
# 3. 缺槽位：写入待办 + 确定性追问（不花模型调用）
# ============================================================================


def test_missing_slot_asks_back_without_calling_the_model() -> None:
    llm = ScriptedChatModel.scripted(AIMessage(content="这条回复不该被用到"))
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, BARE_SCORE_QUESTION)

    # 追问是确定性的文案，本轮就只输出它
    assert reply_of(state) == slot_question(SLOT_SEMESTER)
    # 任务状态被写进 State（下一步就靠它跨轮恢复）
    assert state["pending_intent"] == SCORE_QUERY_INTENT
    assert state["pending_slot"] == SLOT_SEMESTER
    assert state["pending_value"] is None
    # 缺槽位这一步不消耗模型、不执行工具
    assert llm.calls == 0
    assert tool_messages(state) == []
    # 条件边：待办未补齐 -> 本轮结束
    assert route_after_task_slot(state) == END


def test_asking_back_does_not_push_a_system_prompt_into_history() -> None:
    """追问轮次不写任何系统提示进状态（否则 checkpointer 里会逐轮堆积）。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="不该出现"))
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, BARE_SCORE_QUESTION)

    assert not has_persisted_system_prompt(state)
    assert human_texts(state) == [BARE_SCORE_QUESTION]


# ============================================================================
# 4. 已带范围的请求不被拦截（不回归）
# ============================================================================


def test_scoped_score_query_goes_straight_to_the_tool() -> None:
    """带课程的成绩问题不被拦下追问学期，行为与改造前完全一致。"""

    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"course": "数据结构"}),
        AIMessage(content="数据结构 92 分。"),
    )
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, "数据结构考了多少分？")

    assert state.get("pending_slot") is None
    executed = tool_messages(state)
    assert [message.name for message in executed] == ["get_score"]
    assert '"score": 92.0' in executed[0].content
    assert llm.calls == 2
    assert reply_of(state) == "数据结构 92 分。"


@pytest.mark.parametrize(
    "question",
    ["我最近有什么考试？", "我这周有什么课？", "图书馆几点开门"],
)
def test_non_score_queries_are_never_intercepted(question: str) -> None:
    llm = ScriptedChatModel.scripted(AIMessage(content="已为你查询。"))
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, question)

    assert state.get("pending_slot") is None
    assert state.get("pending_intent") is None
    assert reply_of(state) == "已为你查询。"
    assert llm.calls == 1


@pytest.mark.parametrize(
    "question",
    ["课表、考试、数据结构成绩都给我", "考试和数据结构成绩都给我"],
)
def test_mixed_domain_requests_are_not_intercepted(question: str) -> None:
    """同时要课表 / 考试 / 成绩时不追问学期，直接走原有的多工具链路。

    这是本阶段**有意**的窄口径：拦下一句多域请求去问学期，会把用户已经说出口的
    另外几项丢掉。代价是「课表 + 成绩」这类混合请求拿不到学期补齐——
    留待后续阶段（见完成报告的遗留问题）。
    """

    llm = ScriptedChatModel.scripted(
        tool_call("get_exam_schedule", {}, call_id="c1"),
        AIMessage(content="都已查好。"),
    )
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, question)

    assert state.get("pending_slot") is None
    assert [message.name for message in tool_messages(state)] == ["get_exam_schedule"]
    assert reply_of(state) == "都已查好。"


def test_exam_tool_scope_query_is_not_intercepted() -> None:
    """带课程名 + 成绩的问法（既有回归用例里的原句）不被拦下。"""

    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"course": "数据结构"}, call_id="c2"),
        AIMessage(content="成绩已查到。"),
    )
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, "考试和数据结构成绩都给我")

    assert state.get("pending_slot") is None
    assert [message.name for message in tool_messages(state)] == ["get_score"]


# ============================================================================
# 5. 槽位补齐 -> 恢复任务意图 -> 走完整 Tool Calling 链路
# ============================================================================


def test_filling_the_slot_restores_the_task_and_uses_the_existing_tool_chain() -> None:
    """第二轮只有一个「上学期」：靠待办恢复成查成绩任务，再由 Agent 调工具取数。"""

    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"semester": PREVIOUS}),
        AIMessage(content="上学期你修了高等数学与大学英语。"),
    )
    # 第二轮「上学期」单看什么都不是，孤立识别给出 unknown —— 正是本阶段要解决的问题
    classifier = ScriptedIntentClassifier(SCORE_QUERY_INTENT, "unknown")
    graph = build_task_graph(classifier=classifier, llm=llm)

    first = invoke(graph, BARE_SCORE_QUESTION)
    assert first["pending_slot"] == SLOT_SEMESTER

    second = invoke(graph, SEMESTER_REPLY)

    # (1) 状态被恢复：意图取自待办，而不是本轮那个「unknown」
    assert classifier.seen == [BARE_SCORE_QUESTION, SEMESTER_REPLY]
    assert second["intent"] == SCORE_QUERY_INTENT
    # (2) 槽位被消费掉：待办清空，值用完即置回 None
    assert second["pending_slot"] is None
    assert second["pending_value"] is None
    # (3) 工具真的执行了，且查的是待办里那个学期（服务端按 semester 过滤）
    executed = tool_messages(second)
    assert [message.name for message in executed] == ["get_score"]
    assert executed[0].status == "success"
    assert PREVIOUS in executed[0].content
    assert "高等数学" in executed[0].content and "大学英语" in executed[0].content
    assert "数据结构" not in executed[0].content  # 没把别的学期带进来
    # (4) 最终回答建立在工具结果之上
    assert reply_of(second) == "上学期你修了高等数学与大学英语。"
    assert llm.calls == 2  # agent -> tools -> agent
    # (5) 多轮历史完整累积（checkpointer 未被破坏）
    assert human_texts(second) == [BARE_SCORE_QUESTION, SEMESTER_REPLY]
    # (6) 任务上下文只随请求发出，不写回状态
    assert not has_persisted_system_prompt(second)


def test_task_context_is_handed_to_the_model_then_consumed_once() -> None:
    """上下文只注入一次：补齐的那一轮注入，之后（含工具回边后的第二次调用）不再有。"""

    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"semester": PREVIOUS}),
        AIMessage(content="上学期成绩已列出。"),
        AIMessage(content="不客气。"),
    )
    classifier = ScriptedIntentClassifier(SCORE_QUERY_INTENT, "unknown", SCORE_QUERY_INTENT)
    graph = build_task_graph(classifier=classifier, llm=llm)

    invoke(graph, BARE_SCORE_QUESTION)
    invoke(graph, SEMESTER_REPLY)
    third = invoke(graph, "谢谢")

    # 补齐轮的第一次模型调用带上了上下文
    assert HINT_MARKER in first_prompt(llm, index=0)
    assert PREVIOUS in first_prompt(llm, index=0)
    # 工具回边后的第二次调用已经拿到工具结果，不再需要上下文
    assert HINT_MARKER not in first_prompt(llm, index=1)
    # 下一轮更不该再出现（否则会在 checkpointer 里越滚越多）
    assert HINT_MARKER not in first_prompt(llm, index=2)
    assert third["pending_value"] is None
    assert third["pending_slot"] is None


# ============================================================================
# 6. checkpointer：同 thread 恢复、跨 thread 隔离
# ============================================================================


def test_same_thread_restores_the_task_and_other_threads_do_not() -> None:
    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"semester": PREVIOUS}),
        AIMessage(content="上学期成绩已列出。"),
        AIMessage(content="你好，有什么可以帮你？"),
    )
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    first = invoke(graph, BARE_SCORE_QUESTION, THREAD_A)
    assert first["pending_slot"] == SLOT_SEMESTER

    filled = invoke(graph, SEMESTER_REPLY, THREAD_A)
    assert [message.name for message in tool_messages(filled)] == ["get_score"]
    assert human_texts(filled) == [BARE_SCORE_QUESTION, SEMESTER_REPLY]

    # 另一个 thread 的同一句话：看不到 thread-a 的待办，因此不会被当成补槽位
    other = invoke(graph, SEMESTER_REPLY, THREAD_B)

    assert other.get("pending_slot") is None
    assert other.get("pending_value") is None
    assert tool_messages(other) == []
    assert human_texts(other) == [SEMESTER_REPLY]
    assert reply_of(other) == "你好，有什么可以帮你？"
    # 最强的一条：本轮发给模型的提示与基线**逐字节相同**，任务上下文没有串会话
    assert first_prompt(llm, index=2) == TOOL_AGENT_SYSTEM_PROMPT


def test_pending_task_does_not_leak_into_a_fresh_thread() -> None:
    """新 thread 的第一次请求就是「查成绩」：它应当从零开始，不受老会话影响。"""

    llm = ScriptedChatModel.scripted(AIMessage(content="这条不该被用到"))
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    invoke(graph, BARE_SCORE_QUESTION, THREAD_A)
    fresh = invoke(graph, BARE_SCORE_QUESTION, THREAD_B)

    assert fresh["pending_slot"] == SLOT_SEMESTER
    assert fresh["pending_intent"] == SCORE_QUERY_INTENT
    assert human_texts(fresh) == [BARE_SCORE_QUESTION]
    assert llm.calls == 0


# ============================================================================
# 7. 待办的生命周期
# ============================================================================


def test_unresolved_slot_keeps_asking_without_burning_model_calls() -> None:
    llm = ScriptedChatModel.scripted(AIMessage(content="这条不该被用到"))
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    invoke(graph, BARE_SCORE_QUESTION)
    second = invoke(graph, "不知道")
    third = invoke(graph, "就是想看看")

    assert reply_of(second) == slot_question(SLOT_SEMESTER)
    assert reply_of(third) == slot_question(SLOT_SEMESTER)
    assert third["pending_slot"] == SLOT_SEMESTER
    assert llm.calls == 0


def test_pending_task_is_cancelled_when_the_user_moves_to_knowledge() -> None:
    """用户转去问别的事时取消待办，而不是永远追问下去。"""

    rag = FakeRAGService(answer="图书馆开放时间为周一至周五 8:00–22:00。")
    llm = ScriptedChatModel.scripted(AIMessage(content="这条不该被用到"))
    graph = build_task_graph(
        classifier=ScriptedIntentClassifier(SCORE_QUERY_INTENT, "knowledge"),
        llm=llm,
        rag_service=rag,
    )

    invoke(graph, BARE_SCORE_QUESTION)
    moved = invoke(graph, "图书馆开放时间是怎么规定的？")

    assert moved.get("pending_intent") is None
    assert moved.get("pending_slot") is None
    assert moved.get("pending_value") is None
    assert reply_of(moved) == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert rag.calls and rag.calls[0][0] == "图书馆开放时间是怎么规定的？"
    assert llm.calls == 0  # RAG 分支不调模型、不绑工具


def test_pending_task_is_cancelled_by_a_plain_chat_turn() -> None:
    llm = ScriptedChatModel.scripted(AIMessage(content="你好呀，有什么可以帮你？"))
    graph = build_task_graph(
        classifier=ScriptedIntentClassifier(SCORE_QUERY_INTENT, "chat"),
        llm=llm,
    )

    invoke(graph, BARE_SCORE_QUESTION)
    moved = invoke(graph, "你好")

    assert moved.get("pending_slot") is None
    assert reply_of(moved) == "你好呀，有什么可以帮你？"
    assert llm.calls == 1


# ============================================================================
# 8. 既有链路不回归
# ============================================================================


def test_single_turn_score_query_with_semester_and_course_is_untouched() -> None:
    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"semester": PREVIOUS, "course": "大学英语"}),
        AIMessage(content="上学期大学英语 86 分。"),
    )
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    state = invoke(graph, "上学期大学英语考了多少分？")

    assert state.get("pending_slot") is None
    executed = tool_messages(state)
    assert [message.name for message in executed] == ["get_score"]
    assert "大学英语" in executed[0].content
    assert "高等数学" not in executed[0].content  # course 参数真实生效
    assert reply_of(state) == "上学期大学英语 86 分。"


def test_multi_turn_tool_calls_still_accumulate_across_turns() -> None:
    """没有待办时的普通多轮（课表 + 考试）行为与 Tool Calling 阶段一致。"""

    llm = ScriptedChatModel.scripted(
        tool_call("get_course_schedule", {"week": 3}, call_id="c1"),
        AIMessage(content="这周有 6 节课。"),
        tool_call("get_exam_schedule", {}, call_id="c2"),
        AIMessage(content="还有两门考试。"),
    )
    graph = build_task_graph(classifier=StaticIntentClassifier("query"), llm=llm)

    invoke(graph, "我这周有什么课？", THREAD_A)
    second = invoke(graph, "那最近有什么考试？", THREAD_A)

    assert [message.name for message in tool_messages(second)] == [
        "get_course_schedule",
        "get_exam_schedule",
    ]
    assert reply_of(second) == "还有两门考试。"
    assert second.get("pending_slot") is None
    assert llm.calls == 4


def test_knowledge_and_chat_branches_are_untouched_without_a_pending_task() -> None:
    rag = FakeRAGService(
        answer="图书馆开放时间为周一至周五 8:00–22:00。",
        sources=[{"source": "campus_rules.md", "title": "校园规章", "score": 0.9, "snippet": "…"}],
    )
    llm = ScriptedChatModel.scripted(AIMessage(content="你好，有什么可以帮你？"))

    graph = build_task_graph(
        classifier=StaticIntentClassifier("knowledge"), llm=llm, rag_service=rag
    )
    knowledge_state = invoke(graph, "图书馆开放时间是怎么规定的？")
    assert reply_of(knowledge_state) == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert knowledge_state["sources"]
    assert llm.calls == 0

    chat_graph = build_task_graph(classifier=StaticIntentClassifier("chat"), llm=llm)
    chat_state = invoke(chat_graph, "你好")
    assert reply_of(chat_state) == "你好，有什么可以帮你？"
    assert llm.calls == 1


# ============================================================================
# 9. 架构守卫
# ============================================================================


def test_graph_topology_routes_through_the_task_slot_node() -> None:
    source = Path(graph_module.__file__).read_text(encoding="utf-8")

    assert TASK_SLOT_NODE == "task_slot"
    assert "builder.add_node(TASK_SLOT_NODE, create_task_slot_node())" in source
    assert "builder.add_edge(INTENT_ROUTER_NODE, TASK_SLOT_NODE)" in source
    assert (
        "builder.add_conditional_edges(TASK_SLOT_NODE, route_after_task_slot, "
        "SLOT_ROUTE_TARGETS)" in source
    )
    # 路由权已移交：旧接线不得残留（否则条件边会有两个源，行为不再唯一）
    assert "add_conditional_edges(INTENT_ROUTER_NODE" not in source
    assert END in SLOT_ROUTE_TARGETS.values()
    assert set(ROUTE_TARGETS) <= set(SLOT_ROUTE_TARGETS)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"pending_slot": SLOT_SEMESTER, "intent": "query"}, END),
        ({"pending_slot": None, "intent": "query"}, AGENT_NODE),
        ({"pending_slot": None, "intent": "action"}, AGENT_NODE),
        ({"pending_slot": None, "intent": "chat"}, CHAT_NODE),
        ({"pending_slot": None, "intent": "knowledge"}, KNOWLEDGE_NODE),
        ({"pending_slot": None, "intent": "unknown"}, UNKNOWN_NODE),
        ({"pending_slot": None, "intent": "不存在"}, UNKNOWN_NODE),
        ({}, UNKNOWN_NODE),
    ],
)
def test_route_after_task_slot_only_short_circuits_while_waiting(
    state: dict[str, Any], expected: str
) -> None:
    assert route_after_task_slot(state) == expected


def test_task_slot_node_never_touches_model_tools_or_the_network() -> None:
    """槽位节点只读写状态：不建模型、不执行工具、不发请求。"""

    source = Path(task_slot_module.__file__).read_text(encoding="utf-8")
    import_lines = [
        line
        for line in source.splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]
    assert import_lines, "未扫描到 import 行，守卫用例失效"

    forbidden = (
        "BaseChatModel",
        "get_llm",
        "app.tools",
        "httpx",
        "campus_api",
        "chroma",
        "vectorstore",
        "embeddings",
        "retriever",
        "rag.",
    )
    for line in import_lines:
        lowered = line.lower()
        for token in forbidden:
            assert token.lower() not in lowered, f"task_slot 出现越界依赖: {line.strip()}"

    # 不调用任何模型 / 结构化输出（追问是确定性文案）
    assert ".invoke(" not in source
    assert "with_structured_output" not in source


def test_slots_module_has_no_runtime_dependencies() -> None:
    """槽位与学期解析必须是纯函数模块：顶层只允许标准库导入。"""

    source = Path(slots_module.__file__).read_text(encoding="utf-8")
    module_imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    assert module_imports, "未扫描到顶层 import，守卫用例失效"

    for line in module_imports:
        for token in ("langchain", "langgraph", "pydantic", "httpx", "app."):
            assert token not in line, f"slots.py 顶层出现依赖: {line.strip()}"

    # 「当前学期」只有一个来源（配置层），且是惰性导入（模块导入不读配置）；
    # 本模块不得再维护任何日历常量。
    assert "from app.config.settings import get_campus_calendar_settings" in source
    assert "CURRENT_SEMESTER =" not in source
    assert "CURRENT_SEMESTER:" not in source
    assert ".invoke(" not in source


def test_task_context_field_is_read_by_the_agent_node_only_once() -> None:
    """守卫：消费任务上下文的只有 agent 节点一处，且消费后必须置回 None。"""

    source = Path(tool_agent_module.__file__).read_text(encoding="utf-8")

    assert 'slot_hint(state.get("pending_value"))' in source
    assert source.count('"pending_value": None') >= 2  # 失败分支与正常分支都要清空
    # 上下文只加在系统提示里，不写进消息历史
    assert "TOOL_AGENT_SYSTEM_PROMPT" in source
