"""成绩查询改走 Campus MCP Server 的测试（完全离线）。

本阶段把 ``get_score`` 从「普通 Tool」改成「经现有 Campus MCP Server 调用」，
链路变为::

    Agent -> MCP Client -> Campus MCP Server -> CampusApiClient -> Spring Boot -> MySQL

该文件按需求逐项覆盖：

======================  ==========================================================
需求项                   对应用例
======================  ==========================================================
1  tools/list 发现三工具  ``test_server_exposes_all_three_tools``
2  get_score schema 正确  ``test_score_tool_schema_shape``
3  student_id 不暴露      ``test_bound_student_id_is_not_exposed_to_the_model``
4  student_id 不可覆盖    ``test_model_cannot_override_the_bound_student_id`` /
                         ``test_bound_student_id_is_filled_in_when_the_model_omits_it``
5  semester 可选          ``test_optional_arguments_stay_typed_and_described`` /
                         ``test_missing_optional_arguments_keep_the_query_clean``
6  course 可选            ``test_optional_arguments_stay_typed_and_described``
7  semester 透传          ``test_semester_reaches_the_campus_api_client``
8  course 不进 query      ``test_course_is_not_sent_to_the_service`` /
                         ``test_campus_api_client_takes_no_course_parameter``
9  返回四字段             ``test_score_tool_returns_contract_rows``
10 指定 course 能筛选     ``test_course_argument_filters_the_rows``
11 指定 semester 服务端生效 ``test_semester_filtering_happens_on_the_service``
12 错误被 ToolNode 处理   ``test_toolnode_turns_score_mcp_failure_into_an_error_message``
                         （外加 4 条服务端失败翻译用例）
13 exam MCP 不回归        ``test_exam_slot_is_unchanged``
14 course MCP 不回归      ``test_course_slot_is_unchanged``
15 RAG/chat/routing 不回归 ``test_knowledge_branch_does_not_touch_mcp`` 等
16 MCP 关闭时回退直连      ``test_disabled_mcp_falls_back_to_the_direct_score_tool``
======================  ==========================================================

手法与既有测试一致：**只替换「数据从哪来」这一处**。
- 服务端用例把 ``app.mcp.server`` 命名空间里的 ``get_campus_api_client`` 换成挂在
  ``httpx.MockTransport`` 上的替身，Server 自身的注册、schema、参数绑定、
  结果整理、异常翻译全部真实执行；
- 适配层与装配用例用 ``FakeMCPClient``（与 ``StdioMCPClient`` 同名同签名），
  不起子进程；
- 真实 stdio 协议往返在 ``tests/mcp/test_client_stdio.py``（回环 stub 替代 Spring Boot）。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.server.mcpserver.exceptions import ToolError

from app.agent.graph import AGENT_NODE, KNOWLEDGE_NODE, build_graph, route_by_intent
from app.agent.nodes.tool_agent import AGENT_TOOLS
from app.infrastructure.campus_api import CampusApiClient, CampusServiceUnavailable
from app.mcp import server as server_module
from app.mcp import tools as tools_module
from app.mcp.client import (
    MCPServerUnavailable,
    MCPToolInvocationError,
    MCPToolNotFound,
)
from app.mcp.server import (
    TOOL_GET_COURSE_SCHEDULE,
    TOOL_GET_EXAM_SCHEDULE,
    TOOL_GET_SCORE,
    TOOL_SUBMIT_LEAVE_APPLICATION,
    build_server,
)
from app.mcp.tools import (
    COURSE_TOOL_NAME,
    EXAM_TOOL_NAME,
    MCPToolAdapter,
    SCORE_TOOL_BOUND_ARGUMENT,
    SCORE_TOOL_NAME,
    build_agent_tools,
    build_score_mcp_tool,
    make_mcp_tool,
)
from app.tools import get_score
from tests._fakes import (
    COURSE_SCHEDULE_PAYLOAD,
    DEFAULT_CAMPUS_PAYLOADS,
    EXAM_SCHEDULE_PAYLOAD,
    SCORE_PAYLOAD,
    FakeMCPClient,
    FakeRAGService,
    ScriptedChatModel,
    StaticIntentClassifier,
    make_campus_client,
    make_mcp_course_tool_definition,
    make_mcp_score_tool_definition,
    make_mcp_server_tool_definitions,
    make_mcp_settings,
    make_mcp_tool_definition,
    patch_campus_api_in,
    score_payload,
)

SCORE_QUESTION = "我数据结构考了多少分？"

CONTRACT_FIELDS = ("semester", "course", "score", "credit")

#: 成绩数据只在 MySQL 里：源码不得出现的业务数据字面量。
#: 刻意**不含**学期串（``2025-2026-1``）——它是**教务日历**上的当前位置，
#: 与 ``CURRENT_WEEK`` 同类（直连工具的 ``CURRENT_SEMESTER`` 也是它），
#: 不属于成绩数据。
SCORE_DATA_LITERALS = (
    "数据结构",
    "计算机网络",
    "操作系统",
    "高等数学",
    "大学英语",
    "92.0",
    "88.0",
    "95.0",
    "86.0",
    "84.0",
)

# --------------------------------------------------------------- 测试用构造


def _list_tools(server: Any) -> list[Any]:
    """按 SDK 方式列出已注册工具（无需活的会话）。"""

    return list(asyncio.run(server.list_tools()))


def _tool_named(server: Any, name: str) -> Any:
    """按名字取工具定义（注册多个工具后不能再靠位置解包）。"""

    for tool in _list_tools(server):
        if tool.name == name:
            return tool
    raise AssertionError(f"Server 未注册工具 {name}")


def _call_score(server: Any, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(TOOL_GET_SCORE, arguments))


def _score_adapter(
    client: FakeMCPClient, *, student_id: str = "student-001"
) -> MCPToolAdapter:
    """构造「已绑定身份」的成绩 MCP 适配器（跳过真实建连）。"""

    return make_mcp_tool(
        client,
        make_mcp_score_tool_definition(),
        bound_arguments={SCORE_TOOL_BOUND_ARGUMENT: student_id},
    )


def _tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _build_test_graph(tools: tuple[Any, ...], llm: ScriptedChatModel, intent: str = "query"):
    return build_graph(
        llm=llm,
        intent_classifier=StaticIntentClassifier(intent),  # type: ignore[arg-type]
        rag_service=FakeRAGService(),  # type: ignore[arg-type]
        tools=tools,
    )


def _invoke(graph: Any, text: str, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def _tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


# ============================================================ 1 / 2. 发现与 schema


def test_server_exposes_all_three_tools() -> None:
    """需求 1：同一次 ``tools/list`` 里能发现考试、课程、成绩（及请假提交）四个工具。"""

    names = [tool.name for tool in _list_tools(build_server())]

    assert names == [
        TOOL_GET_EXAM_SCHEDULE,
        TOOL_GET_COURSE_SCHEDULE,
        TOOL_GET_SCORE,
        TOOL_SUBMIT_LEAVE_APPLICATION,
    ]
    assert TOOL_GET_SCORE == SCORE_TOOL_NAME


def test_score_tool_schema_shape() -> None:
    """需求 2：``student_id`` 必填 string 且带说明；``semester`` / ``course`` 可选、带说明。"""

    schema = _tool_named(build_server(), TOOL_GET_SCORE).input_schema

    assert schema["type"] == "object"
    properties = schema["properties"]
    assert properties["student_id"]["type"] == "string"
    assert properties["student_id"]["description"]
    assert "student_id" in schema["required"]

    for optional in ("semester", "course"):
        # 可选参数的真实形态是「string 或 null」，说明嵌在分支里
        alternatives = properties[optional]["anyOf"]
        assert {alt.get("type") for alt in alternatives} == {"string", "null"}
        typed_branch = next(alt for alt in alternatives if alt.get("type") == "string")
        assert typed_branch["description"]
        assert optional not in schema.get("required", [])


def test_score_tool_description_mentions_fields_and_realtime() -> None:
    """描述要告诉模型字段与「实时查」，否则它会凭记忆编造成绩。"""

    description = _tool_named(build_server(), TOOL_GET_SCORE).description or ""

    assert "成绩" in description
    assert "实时" in description
    for field_name in CONTRACT_FIELDS:
        assert field_name in description


# ==================================== 3 / 4. student_id 由宿主绑定、不暴露给模型


def test_bound_student_id_is_not_exposed_to_the_model() -> None:
    """需求 3：``student_id`` 不出现在模型可见的参数表里，两个可选参数仍在。"""

    adapter = _score_adapter(FakeMCPClient())

    properties = adapter.args_schema.model_json_schema().get("properties", {})

    assert "student_id" not in properties
    assert "studentId" not in properties
    assert set(properties) == {"semester", "course"}


def test_model_cannot_override_the_bound_student_id() -> None:
    """需求 3：绑定身份优先于同名入参——不存在查询他人成绩的入口。"""

    client = FakeMCPClient(payload=SCORE_PAYLOAD)
    adapter = _score_adapter(client, student_id="student-001")

    adapter.invoke({"student_id": "student-002", "semester": "2025-2026-1"})

    assert client.calls == [
        (SCORE_TOOL_NAME, {"student_id": "student-001", "semester": "2025-2026-1"})
    ]


def test_bound_student_id_is_filled_in_when_the_model_omits_it() -> None:
    """需求 3：模型不需要（也无法）提供学号，适配层用配置补齐。"""

    client = FakeMCPClient(payload=SCORE_PAYLOAD)
    adapter = _score_adapter(client, student_id="student-777")

    payload = adapter.invoke({})

    assert payload == SCORE_PAYLOAD
    assert client.calls == [(SCORE_TOOL_NAME, {"student_id": "student-777"})]


# ==================================== 5 / 6. 两个可选参数的 schema 与缺省


def test_optional_arguments_stay_typed_and_described() -> None:
    """需求 2 / 5 / 6：可选参数不能被降级成 ``Any``，也不能丢掉说明。

    MCP SDK 为可选参数生成 ``anyOf(string|null)``，说明嵌在分支里。
    适配层若不解析这种形态，``semester`` / ``course`` 会变成无类型、无说明的参数，
    模型就失去「当前是哪个学期」「支持模糊匹配」这两条关键提示。
    """

    adapter = _score_adapter(FakeMCPClient())
    described = adapter.args_schema.model_json_schema()
    properties = described["properties"]

    for optional in ("semester", "course"):
        spec = properties[optional]
        assert {alt.get("type") for alt in spec["anyOf"]} == {"string", "null"}
        assert spec.get("default") is None
        assert optional not in described.get("required", [])

    assert "学期" in properties["semester"]["description"]
    assert "课程" in properties["course"]["description"]
    # 两个都不传时校验通过并都给出 None（即「不限学期 / 不限课程」）
    assert adapter.args_schema.model_validate({}).model_dump() == {
        "semester": None,
        "course": None,
    }


def test_missing_optional_arguments_keep_the_query_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 5 / 6：两个可选参数都不传时，请求里一个都不出现。"""

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests),
    )

    result = _call_score(build_server(), {"student_id": "student-001"})

    assert dict(requests[0].url.params) == {"studentId": "student-001"}
    assert result.structured_content == {"result": SCORE_PAYLOAD}


# ==================================== 7 / 8. 参数透传与「course 不进请求」


def test_semester_reaches_the_campus_api_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 7：``semester`` 原样透传给 ``CampusApiClient``（过滤由服务端做）。"""

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests),
    )

    result = _call_score(
        build_server(), {"student_id": "student-001", "semester": "2024-2025-2"}
    )

    assert len(requests) == 1, "MCP 工具必须真的经 CampusApiClient 发起了请求"
    assert requests[0].url.path == "/api/scores"
    assert dict(requests[0].url.params) == {
        "studentId": "student-001",
        "semester": "2024-2025-2",
    }
    assert result.is_error is False


def test_course_is_not_sent_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 8：``course`` **不得**作为 query 参数发给 Spring Boot。

    ``/api/scores`` 不提供课程参数；把 ``course`` 塞进请求会让服务端拿到一个
    它不认识的参数，也让「课程筛选在 Python 侧」这条约定悄悄失效。
    """

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests),
    )

    _call_score(build_server(), {"student_id": "student-001", "course": "计算机网络"})

    params = dict(requests[0].url.params)
    assert "course" not in params
    assert params == {"studentId": "student-001"}


def test_campus_api_client_takes_no_course_parameter() -> None:
    """需求 8 的契约侧：``fetch_scores`` 的签名里本来就没有 ``course``。

    这条直接固化「不需要为了 MCP 在 Java API 中增加 course 查询参数」——
    一旦有人往客户端加 ``course``，这里的断言会先失败。
    """

    parameters = inspect.signature(CampusApiClient.fetch_scores).parameters

    assert "course" not in parameters
    assert set(parameters) == {"self", "student_id", "semester"}


# ==================================== 9 / 10 / 11. 返回结构与两个参数的过滤位置


def test_score_tool_returns_contract_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 9：返回结构即对外契约——四字段，分数与学分是**数值**。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_score(build_server(), {"student_id": "student-001"})
    rows = result.structured_content["result"]

    assert rows == SCORE_PAYLOAD
    for row in rows:
        assert list(row) == list(CONTRACT_FIELDS)
        assert isinstance(row["score"], float)
        assert isinstance(row["credit"], float)


def test_course_argument_filters_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 10：指定 ``course`` 时结果被正确筛选（匹配发生在 Python 侧）。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_score(build_server(), {"student_id": "student-001", "course": "计算机网络"})
    rows = result.structured_content["result"]

    assert [row["course"] for row in rows] == ["计算机网络"]
    assert rows[0]["score"] == 88.0
    assert rows[0]["credit"] == 3.0


def test_course_filter_matches_bidirectionally(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 10：课程名匹配是双向包含（模型补全成「某课课程」也能命中）。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_score(
        build_server(), {"student_id": "student-001", "course": "网络"}
    )

    assert [row["course"] for row in result.structured_content["result"]] == ["计算机网络"]


def test_course_filter_returns_empty_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 10：没有匹配的课程时返回空数组，而不是失败。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_score(build_server(), {"student_id": "student-001", "course": "量子力学"})

    assert result.is_error is False
    assert result.structured_content == {"result": []}


def test_semester_filtering_happens_on_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 11：``semester`` 过滤在**服务端**生效（替身按 query 过滤，规则与服务端一致）。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_score(
        build_server(), {"student_id": "student-001", "semester": "2024-2025-2"}
    )
    rows = result.structured_content["result"]

    assert rows == score_payload(semester="2024-2025-2")
    assert {row["semester"] for row in rows} == {"2024-2025-2"}
    # 上学期只有两门，且不含本学期课程
    assert {row["course"] for row in rows} == {"高等数学", "大学英语"}


def test_empty_result_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """该学生没成绩返回空数组，而不是失败。"""

    patch_campus_api_in(monkeypatch, server_module, make_campus_client(payload=[]))

    result = _call_score(build_server(), {"student_id": "student-001"})

    assert result.is_error is False
    assert result.structured_content == {"result": []}


# ============================================================ 12. 失败路径


def test_score_business_failure_is_raised_as_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务失败必须走 ``ToolError``——只有它会把原始消息交给客户端/模型。"""

    class FailingClient:
        def fetch_scores(
            self, student_id: str | None = None, semester: str | None = None
        ) -> list[dict[str, Any]]:
            raise CampusServiceUnavailable("校园业务服务暂时不可用，请稍后再试。")

    patch_campus_api_in(monkeypatch, server_module, FailingClient())  # type: ignore[arg-type]

    with pytest.raises(ToolError) as excinfo:
        _call_score(build_server(), {"student_id": "student-001"})

    assert "校园业务服务暂时不可用" in str(excinfo.value)


def test_http_5xx_is_translated_into_a_readable_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 12：Spring Boot 500 被翻译成可读中文的 ``ToolError``（不是协议原文）。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(status_code=500),
    )

    with pytest.raises(ToolError) as excinfo:
        _call_score(build_server(), {"student_id": "student-001"})

    message = str(excinfo.value)
    assert "校园业务服务内部错误" in message
    assert "Traceback" not in message


def test_data_format_error_is_translated_into_a_readable_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 12：返回结构异常（分数不是数值）同样走可读的 ``ToolError``。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(
            payload=[{"semester": "2025-2026-1", "course": "微观经济学", "score": "甲", "credit": 2.0}]
        ),
    )

    with pytest.raises(ToolError) as excinfo:
        _call_score(build_server(), {"student_id": "student-001"})

    assert "成绩数据格式异常" in str(excinfo.value)


def test_missing_required_argument_is_rejected_by_the_schema() -> None:
    """缺 ``student_id`` 由 SDK 的入参校验拦截（工具不必自己判断）。"""

    with pytest.raises(ToolError):
        _call_score(build_server(), {})


def test_toolnode_turns_score_mcp_failure_into_an_error_message() -> None:
    """需求 12：MCP 侧失败被转成 error ``ToolMessage``，**整轮对话不崩**。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        ),
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        _tool_call(SCORE_TOOL_NAME, {"course": "数据结构"}),
        AIMessage(content="成绩暂时查不到，请稍后再试。"),
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, SCORE_QUESTION, "t-mcp-score-fail")

    executed = _tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "校园业务服务暂时不可用" in executed[0].content
    assert "Traceback" not in executed[0].content
    # 图正常走完，模型据错误消息作答
    assert state["messages"][-1].content == "成绩暂时查不到，请稍后再试。"


# ==================================== 适配层：共用会话、工具链可用性、失败传递


def test_score_mcp_tool_shares_the_session_with_the_other_tools() -> None:
    """三个查询工具跑在同一条会话上：**不为成绩另起第二个 Server 进程**。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={SCORE_TOOL_NAME: SCORE_PAYLOAD},
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    # 只调用三个查询槽位（它们对模型均无参或可选参）；请假提交（写操作）
    # 与它们共用同一条会话这件事由 tests/mcp/test_leave_tool.py 单独守住。
    for tool in tools[:3]:
        tool.invoke({})

    # 三次调用都落在同一个客户端对象上 = 同一条会话
    assert [name for name, _ in client.calls] == [
        EXAM_TOOL_NAME,
        COURSE_TOOL_NAME,
        SCORE_TOOL_NAME,
    ]
    # 三个查询槽位各调用一次 ``start()``（请假槽位的构建也会调，见下）；
    # 真实客户端幂等，因此仍然只建一条会话
    # （``StdioMCPClient.start`` 的幂等性由 ``test_client_stdio.py`` 直接守住）
    assert client.start_count == 3 + 1  # 另一次来自请假工具的构建


def test_score_mcp_tool_is_usable_by_langchain_function_calling() -> None:
    """LangChain 能把成绩适配器转成函数调用 schema（``bind_tools`` 走的就是它）。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    converted = convert_to_openai_tool(tools[2])

    assert converted["function"]["name"] == SCORE_TOOL_NAME
    assert converted["function"]["description"]
    parameters = converted["function"]["parameters"]
    assert parameters["type"] == "object"
    assert set(parameters["properties"]) == {"semester", "course"}
    assert "student_id" not in parameters["properties"], "身份参数不得暴露给模型"


def test_score_mcp_adapter_delegates_failures_without_swallowing_them() -> None:
    """适配层只传递失败，不吞掉（由 ``handle_tool_error`` 统一转成 ToolMessage）。"""

    client = FakeMCPClient(
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        )
    )
    adapter = _score_adapter(client)

    with pytest.raises(MCPToolInvocationError) as excinfo:
        adapter.invoke({"course": "数据结构"})

    assert "校园业务服务暂时不可用" in str(excinfo.value)


# ==================================== 端到端成功路径


def test_score_service_end_to_end_success() -> None:
    """端到端：Agent -> ToolNode -> MCP 适配器 -> 客户端，成功路径。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={SCORE_TOOL_NAME: SCORE_PAYLOAD},
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        _tool_call(SCORE_TOOL_NAME, {"course": "数据结构"}),
        AIMessage(content="数据结构 92 分。"),
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, SCORE_QUESTION, "t-mcp-score-ok")

    executed = _tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "success"
    assert executed[0].name == SCORE_TOOL_NAME
    assert "数据结构" in executed[0].content
    assert '"score": 92.0' in executed[0].content
    # 参数经适配层补齐学号后到达客户端
    assert client.calls == [(SCORE_TOOL_NAME, {"student_id": "student-001", "course": "数据结构"})]
    assert state["messages"][-1].content == "数据结构 92 分。"


# ==================================== 装配：槽位替换与回退粒度


def test_enabled_mcp_replaces_all_three_slots() -> None:
    """三个查询槽位都被 MCP 适配器替换；请假提交是**追加**的第 4 个工具。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert [tool.name for tool in tools[:3]] == [tool.name for tool in AGENT_TOOLS]
    assert all(isinstance(tool, MCPToolAdapter) for tool in tools[:3])
    assert [tool.remote_name for tool in tools[:3]] == [
        EXAM_TOOL_NAME,
        COURSE_TOOL_NAME,
        SCORE_TOOL_NAME,
    ]
    # 清单长度不是 3：请假提交被追加（它没有同名直连工具可顶替）
    assert [tool.name for tool in tools][3:] == ["submit_leave_application"]


def test_disabled_mcp_falls_back_to_the_direct_score_tool() -> None:
    """需求 16：``CAMPUS_MCP_ENABLED=false`` 时成绩回到原来的直连工具，且不建连。"""

    client = FakeMCPClient()
    tools = build_agent_tools(settings=make_mcp_settings(enabled=False), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert tools[2] is get_score
    assert not isinstance(tools[2], MCPToolAdapter)
    assert client.start_count == 0, "未启用时不该有任何 MCP 建连动作"


def test_direct_score_tool_still_works_when_mcp_is_disabled() -> None:
    """需求 16 的行为侧：回退后的直连工具仍能正常查询（离线替身服务）。"""

    assert get_score.invoke({"semester": "2025-2026-1"}) == score_payload(
        semester="2025-2026-1"
    )
    assert get_score.invoke({"course": "计算机网络"}) == [
        row for row in SCORE_PAYLOAD if row["course"] == "计算机网络"
    ]


def test_score_slot_falls_back_alone_when_only_it_is_missing() -> None:
    """回退粒度按工具：服务端没注册成绩工具时，只有成绩回退、另两个仍走 MCP。

    这条守住「MCP 少注册一个工具不该把已可用的能力一起降级」。
    """

    client = FakeMCPClient(
        tools=(make_mcp_tool_definition(), make_mcp_course_tool_definition())
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert isinstance(tools[0], MCPToolAdapter) and tools[0].remote_name == EXAM_TOOL_NAME
    assert isinstance(tools[1], MCPToolAdapter) and tools[1].remote_name == COURSE_TOOL_NAME
    assert tools[2] is get_score


def test_score_slot_missing_is_reported_by_the_builder() -> None:
    """服务端没有成绩工具时，构建函数明确抛 ``MCPToolNotFound``（不静默返回假工具）。"""

    with pytest.raises(MCPToolNotFound):
        build_score_mcp_tool(
            FakeMCPClient(tools=(make_mcp_tool_definition(),)),
            settings=make_mcp_settings(),
        )


def test_unavailable_server_falls_back_every_slot_to_direct() -> None:
    """MCP 整体不可用时三个槽位都回退——等价于改造前的「整体回退」。"""

    client = FakeMCPClient(
        start_error=MCPServerUnavailable("校园 MCP 服务暂时不可用，请稍后再试。")
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert all(not isinstance(tool, MCPToolAdapter) for tool in tools)


# ============================================================ 13 / 14 / 15. 回归


def test_exam_slot_is_unchanged() -> None:
    """需求 13：考试槽位的发现、绑定与调用行为与改造前一致。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={EXAM_TOOL_NAME: EXAM_SCHEDULE_PAYLOAD},
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    exam = tools[0]

    assert isinstance(exam, MCPToolAdapter)
    assert exam.remote_name == EXAM_TOOL_NAME
    # 考试工具对模型完全无参（身份被绑定），与改造前一致
    assert exam.args_schema.model_json_schema().get("properties", {}) == {}
    assert exam.invoke({}) == EXAM_SCHEDULE_PAYLOAD
    assert client.calls == [(EXAM_TOOL_NAME, {"student_id": "student-001"})]


def test_course_slot_is_unchanged() -> None:
    """需求 14：课程槽位的可选 ``week`` 语义与改造前一致（只有 ``week`` 暴露给模型）。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={COURSE_TOOL_NAME: COURSE_SCHEDULE_PAYLOAD},
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    course = tools[1]

    assert isinstance(course, MCPToolAdapter)
    assert course.remote_name == COURSE_TOOL_NAME
    assert set(course.args_schema.model_json_schema()["properties"]) == {"week"}
    assert course.invoke({"week": 3}) == COURSE_SCHEDULE_PAYLOAD
    assert client.calls == [(COURSE_TOOL_NAME, {"student_id": "student-001", "week": 3})]


def test_knowledge_branch_does_not_touch_mcp() -> None:
    """需求 15：RAG 分支既不碰模型也不碰 MCP（一次调用都没有）。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    starts_before = client.start_count
    rag = FakeRAGService(answer="图书馆开放时间为周一至周五 8:00–22:00。")
    graph = build_graph(
        llm=ScriptedChatModel.scripted(AIMessage(content="不该被调用")),
        intent_classifier=StaticIntentClassifier("knowledge"),  # type: ignore[arg-type]
        rag_service=rag,  # type: ignore[arg-type]
        tools=tools,
    )

    state = _invoke(graph, "图书馆开放时间是什么？", "t-mcp-score-rag")

    assert state["messages"][-1].content == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert rag.calls, "knowledge 分支应当走 RAG"
    assert client.calls == [], "RAG 分支不得触发任何 MCP 工具调用"
    assert client.start_count == starts_before
    assert _tool_messages(state) == []


def test_chat_branch_does_not_touch_mcp() -> None:
    """需求 15：chat 分支不绑工具、不碰 MCP。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    graph = _build_test_graph(
        tools,
        ScriptedChatModel.scripted(AIMessage(content="你好！有什么可以帮你？")),
        intent="chat",
    )

    state = _invoke(graph, "你好", "t-mcp-score-chat")

    assert state["messages"][-1].content == "你好！有什么可以帮你？"
    assert client.calls == []
    assert _tool_messages(state) == []


def test_intent_routing_is_unchanged() -> None:
    """需求 15：意图到节点的映射未变（query/action -> agent，knowledge -> knowledge）。"""

    assert route_by_intent({"intent": "query"}) == AGENT_NODE
    assert route_by_intent({"intent": "knowledge"}) == KNOWLEDGE_NODE


# ============================================================ 结构守卫


def test_mcp_server_source_has_no_score_data_literals() -> None:
    """成绩数据只在 MySQL 里：MCP Server 源码不得出现成绩数据字面量。"""

    source = Path(server_module.__file__).read_text(encoding="utf-8")

    for literal in SCORE_DATA_LITERALS:
        assert literal not in source, f"MCP Server 出现业务数据字面量 {literal!r}"


def test_mcp_adapter_source_has_no_score_data_literals() -> None:
    """适配层同理：只做协议转换，不携带业务数据。"""

    source = Path(tools_module.__file__).read_text(encoding="utf-8")

    for literal in SCORE_DATA_LITERALS:
        assert literal not in source, f"适配层出现业务数据字面量 {literal!r}"
