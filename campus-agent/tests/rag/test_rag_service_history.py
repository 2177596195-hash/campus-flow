"""RAGService 历史裁剪的回归测试：孤立 ``tool_calls`` 不得进入 RAG 请求。

背景（真实故障）：上一轮是 Tool Calling（如「帮我查一下考试」）时，历史里会有
``AIMessage(tool_calls=...)`` 与 ``ToolMessage`` 一对消息；下一轮问知识库问题，
``RAGService._trim_history`` 过滤了 ``ToolMessage`` 却保留了带 ``tool_calls`` 的
``AIMessage``。发给 DeepSeek 的请求于是出现「没有对应工具结果的 tool_calls」，
服务端直接返回 HTTP 400（纯知识问题正常、考试查询后紧跟知识问题必现）。

修复后 ``_trim_history`` 的过滤规则：

1. ``ToolMessage`` → 过滤（原有规则，不变）；
2. ``AIMessage`` 且 ``tool_calls`` 非空 → 过滤（本次新增）；
3. 普通 ``AIMessage`` → 保留（原有规则，不变）；
4. ``HumanMessage`` → 按原有规则保留；
5. 最近 ``history_window`` 条的窗口裁剪 → 原有规则，不变。

全部用例离线确定性运行：检索器用最小替身，模型用 ``ScriptedChatModel``
（可精确断言「模型实际收到了哪些消息」），不触达任何真实 LLM。
"""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.rag.schema import RetrievedChunk
from app.rag.service import RAGService
from app.agent.nodes.knowledge import create_knowledge_node
from tests._fakes import ScriptedChatModel

#: 固定命中的知识片段（图书馆开放时间）
CHUNK = RetrievedChunk(
    content="图书馆开放时间为周一至周五 8:00–22:00。",
    source="campus_rules.md",
    title="校园规章",
    score=0.9,
)


class StubRetriever:
    """``RAGService`` 依赖的最小检索器替身：对任何查询都返回同一批片段。

    ``RAGService.retrieve`` 只调用 ``retriever.retrieve(query, top_k=...)``，
    因此这里实现同名方法即可；同时记录收到的查询，便于断言检索入口未被改动。
    """

    def __init__(self, chunks: tuple[RetrievedChunk, ...] = (CHUNK,)) -> None:
        self.chunks = list(chunks)
        self.queries: list[str] = []

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        self.queries.append(query)
        return self.chunks


def make_tool_calling_history() -> list[AnyMessage]:
    """构造「上一轮考试查询」产生的完整 Tool Calling 历史。

    形态与 LangGraph ToolNode 的真实产物一致：用户消息 →
    ``AIMessage(tool_calls=...)`` → ``ToolMessage`` → 最终文本回复。
    """

    return [
        HumanMessage(content="帮我查一下考试"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_exam_schedule",
                    "args": {"student_id": "student-001"},
                    "id": "call_exam_1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content='[{"course": "计算机网络", "date": "2026-09-28"}]',
            tool_call_id="call_exam_1",
            name="get_exam_schedule",
        ),
        AIMessage(content="你近期有两场考试：计算机网络、数据结构。"),
    ]


# ---------------------------------------------------------------- 裁剪规则

def test_trim_history_filters_tool_message() -> None:
    """规则 1：ToolMessage 被过滤（原有行为不得回退）。"""

    service = RAGService(StubRetriever(), history_window=6)
    trimmed = service._trim_history(make_tool_calling_history())

    assert trimmed, "裁剪结果不应为空（还有 Human / 普通 AIMessage 要保留）"
    assert all(not isinstance(message, ToolMessage) for message in trimmed)


def test_trim_history_filters_ai_message_with_tool_calls() -> None:
    """规则 2：带 tool_calls 的 AIMessage 被过滤——修复的核心断言。"""

    service = RAGService(StubRetriever(), history_window=6)
    trimmed = service._trim_history(make_tool_calling_history())

    orphan_calls = [
        message
        for message in trimmed
        if isinstance(message, AIMessage) and message.tool_calls
    ]
    assert not orphan_calls, (
        "带 tool_calls 的 AIMessage 残留在 RAG 历史里，"
        "对应的 ToolMessage 已被过滤，会产生孤立 tool_calls 并触发 DeepSeek 400"
    )


def test_trim_history_keeps_plain_ai_message() -> None:
    """规则 3：普通 AIMessage（纯文本回复）保留。"""

    service = RAGService(StubRetriever(), history_window=6)
    trimmed = service._trim_history(make_tool_calling_history())

    plain_ai = [message for message in trimmed if isinstance(message, AIMessage)]
    assert len(plain_ai) == 1
    assert plain_ai[0].content == "你近期有两场考试：计算机网络、数据结构。"
    assert not plain_ai[0].tool_calls


def test_trim_history_keeps_human_message() -> None:
    """规则 4：HumanMessage 按原有规则保留。"""

    service = RAGService(StubRetriever(), history_window=6)
    trimmed = service._trim_history(make_tool_calling_history())

    human = [message for message in trimmed if isinstance(message, HumanMessage)]
    assert [message.content for message in human] == ["帮我查一下考试"]


def test_history_window_slicing_unchanged() -> None:
    """规则 5：窗口裁剪逻辑不变——仍取最近 ``history_window`` 条。"""

    history: list[AnyMessage] = [HumanMessage(content=f"问题{index}") for index in range(5)]
    history.append(AIMessage(content="回答"))
    service = RAGService(StubRetriever(), history_window=3)

    trimmed = service._trim_history(history)

    assert [message.content for message in trimmed] == ["问题3", "问题4", "回答"]


# ---------------------------------------------------------------- answer 链路

def test_answer_plain_knowledge_question() -> None:
    """普通知识问题（无工具历史）：请求形态与回答结果均正常。"""

    model = ScriptedChatModel.scripted(AIMessage(content="图书馆周一至周五 8:00–22:00 开放。"))
    service = RAGService(StubRetriever(), llm=model)

    result = service.answer("图书馆几点开门？")

    assert result.answer == "图书馆周一至周五 8:00–22:00 开放。"
    assert result.retrieved == 1
    assert result.sources
    assert model.calls == 1
    messages = model.received[0]
    # 无历史时请求只有两条：System（提示词 + 检索上下文）与 Human（当前问题）
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "图书馆几点开门？"


def test_answer_after_tool_calling_round_has_no_orphan_tool_calls() -> None:
    """考试查询 → 紧接知识问题：发给模型的消息里不得再有孤立 tool_calls。"""

    model = ScriptedChatModel.scripted(AIMessage(content="图书馆周一至周五 8:00–22:00 开放。"))
    service = RAGService(StubRetriever(), llm=model)

    result = service.answer("图书馆几点关门？", history=make_tool_calling_history())

    # 链路整体正常：命中片段、有引用、模型被调用且拿到回答
    assert result.retrieved == 1
    assert result.sources
    assert result.answer == "图书馆周一至周五 8:00–22:00 开放。"
    assert model.calls == 1

    messages = model.received[0]
    # 核心回归断言：既无 ToolMessage，也无带 tool_calls 的 AIMessage
    assert not any(isinstance(message, ToolMessage) for message in messages)
    assert not any(
        isinstance(message, AIMessage) and message.tool_calls for message in messages
    )
    # 历史部分只保留普通 Human / AIMessage，当前问题仍在最后
    history_part = messages[1:-1]
    assert [type(message) for message in history_part] == [HumanMessage, AIMessage]
    assert isinstance(messages[-1], HumanMessage)
    assert messages[-1].content == "图书馆几点关门？"


# ---------------------------------------------------------------- 节点级回归

def test_knowledge_node_after_tool_calling_round() -> None:
    """Agent 层回归：knowledge 节点带着 Tool Calling 历史进入 RAG，不再触发 400 形态。

    直接复刻真实故障的两轮对话：第一轮「帮我查一下考试」产生
    ``AIMessage(tool_calls=...)`` + ``ToolMessage``；第二轮「图书馆几点关门？」
    进入 knowledge 节点。断言最终发给模型的请求里没有孤立 tool_calls。
    """

    model = ScriptedChatModel.scripted(AIMessage(content="图书馆周一至周五 8:00–22:00 开放。"))
    service = RAGService(StubRetriever(), llm=model)
    node = create_knowledge_node(service)

    messages = make_tool_calling_history() + [HumanMessage(content="图书馆几点关门？")]
    result = node({"messages": messages}, None)

    assert result["error"] is None
    assert result["sources"]
    assert result["messages"][0].content == "图书馆周一至周五 8:00–22:00 开放。"
    assert model.calls == 1

    sent = model.received[0]
    assert not any(isinstance(message, ToolMessage) for message in sent)
    assert not any(
        isinstance(message, AIMessage) and message.tool_calls for message in sent
    )
    assert isinstance(sent[-1], HumanMessage)
    assert sent[-1].content == "图书馆几点关门？"
