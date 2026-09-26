"""课程查询改走 Campus MCP Server 的测试（完全离线）。

本阶段把 ``get_course_schedule`` 从「普通 Tool」改成「经现有 Campus MCP Server
调用」，链路变为::

    Agent -> MCP Client -> Campus MCP Server -> CampusApiClient -> Spring Boot -> MySQL

该文件按需求逐项覆盖：

======================  ==========================================================
需求项                   对应用例
======================  ==========================================================
1  tools/list 能发现      ``test_server_exposes_exam_and_course_tools``
2  参数 schema 正确        ``test_course_tool_schema_shape`` /
                         ``test_week_stays_optional_integer_with_description``
3  student_id 不暴露/不可覆盖 ``test_bound_student_id_is_not_exposed_to_the_model`` /
                         ``test_model_cannot_override_the_bound_student_id``
4  week 可选              ``test_course_tool_without_week_omits_the_query`` /
                         ``test_week_stays_optional_integer_with_description``
5  正确调用 CampusApiClient ``test_course_tool_call_reaches_the_campus_api_client``
6  参数传到 /api/courses    ``test_course_tool_call_reaches_the_campus_api_client``
7  正常返回课程数据        ``test_course_tool_returns_contract_rows``
8  异常被 ToolNode 处理     ``test_toolnode_turns_course_mcp_failure_into_an_error_message``
9  exam MCP 不回归         ``test_exam_mcp_behaviour_is_unchanged``
10 score 槽位不回归        见 ``tests/mcp/test_score_tool.py``
                         （本阶段成绩已改走 MCP，原「score 仍是直连」断言随之反转）
11 RAG/chat/routing 不回归  ``test_knowledge_branch_does_not_touch_mcp`` 等
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
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.server.mcpserver.exceptions import ToolError

from app.agent.graph import AGENT_NODE, KNOWLEDGE_NODE, build_graph, route_by_intent
from app.agent.nodes.tool_agent import AGENT_TOOLS
from app.infrastructure.campus_api import CampusServiceUnavailable
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
    COURSE_TOOL_BOUND_ARGUMENT,
    COURSE_TOOL_NAME,
    EXAM_TOOL_BOUND_ARGUMENT,
    EXAM_TOOL_NAME,
    MCPToolAdapter,
    build_agent_tools,
    build_course_mcp_tool,
    build_exam_mcp_tool,
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
    course_schedule_payload,
    make_campus_client,
    make_mcp_course_tool_definition,
    make_mcp_server_tool_definitions,
    make_mcp_settings,
    make_mcp_tool_definition,
    patch_campus_api_in,
)

COURSE_QUESTION = "我第3周有什么课？"

CONTRACT_FIELDS = {"course", "teacher", "weekday", "time", "room", "weeks"}

#: 课程工具源码里不得出现的业务数据字面量（数据只在 MySQL 里）
COURSE_DATA_LITERALS = ("数据结构", "计算机网络", "数据库原理", "A302", "B201")

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


def _call_course(server: Any, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(TOOL_GET_COURSE_SCHEDULE, arguments))


def _course_adapter(
    client: FakeMCPClient, *, student_id: str = "student-001"
) -> MCPToolAdapter:
    """构造「已绑定身份」的课程 MCP 适配器（跳过真实建连）。"""

    return make_mcp_tool(
        client,
        make_mcp_course_tool_definition(),
        bound_arguments={COURSE_TOOL_BOUND_ARGUMENT: student_id},
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


def test_server_exposes_exam_and_course_tools() -> None:
    """需求 1：同一次 ``tools/list`` 里能发现课程工具（与考试、成绩、请假同属一台 Server）。"""

    names = [tool.name for tool in _list_tools(build_server())]

    assert names == [
        TOOL_GET_EXAM_SCHEDULE,
        TOOL_GET_COURSE_SCHEDULE,
        TOOL_GET_SCORE,
        TOOL_SUBMIT_LEAVE_APPLICATION,
    ]
    assert TOOL_GET_COURSE_SCHEDULE == COURSE_TOOL_NAME


def test_course_tool_schema_shape() -> None:
    """需求 2：``student_id`` 必填 string 且带说明；``week`` 可选、integer、带说明。"""

    schema = _tool_named(build_server(), TOOL_GET_COURSE_SCHEDULE).input_schema

    assert schema["type"] == "object"
    properties = schema["properties"]
    assert properties["student_id"]["type"] == "string"
    assert properties["student_id"]["description"]
    assert "student_id" in schema["required"]

    # week 可选：出现在 properties、不在 required
    assert "week" in properties
    assert "week" not in schema.get("required", [])
    # week 的真实形态是「integer 或 null」，说明嵌在分支里
    alternatives = properties["week"]["anyOf"]
    assert {alt.get("type") for alt in alternatives} == {"integer", "null"}
    integer_branch = next(alt for alt in alternatives if alt.get("type") == "integer")
    assert integer_branch["description"]


def test_course_tool_description_mentions_fields_and_realtime() -> None:
    """描述要告诉模型字段与「实时查」，否则它会凭记忆编造课表。"""

    description = _tool_named(build_server(), TOOL_GET_COURSE_SCHEDULE).description or ""

    assert "课" in description
    assert "实时" in description
    for field_name in sorted(CONTRACT_FIELDS):
        assert field_name in description


# ==================================== 3. student_id 由宿主绑定、不暴露给模型


def test_bound_student_id_is_not_exposed_to_the_model() -> None:
    """需求 3：``student_id`` 不出现在模型可见的参数表里，``week`` 仍在。"""

    adapter = _course_adapter(FakeMCPClient())

    properties = adapter.args_schema.model_json_schema().get("properties", {})

    assert "student_id" not in properties
    assert "studentId" not in properties
    assert set(properties) == {"week"}


def test_week_stays_optional_integer_with_description() -> None:
    """需求 2 / 4：可选参数不能被降级成 ``Any``，也不能丢掉说明。

    MCP SDK 为可选参数生成 ``anyOf(integer|null)``，说明嵌在分支里。
    适配层若不解析这种形态，``week`` 会变成无类型、无说明的参数，
    模型就失去「当前第 3 教学周」这条关键提示。
    """

    adapter = _course_adapter(FakeMCPClient())
    described = adapter.args_schema.model_json_schema()
    week = described["properties"]["week"]

    assert {alt.get("type") for alt in week["anyOf"]} == {"integer", "null"}
    assert week["description"] and "教学周" in week["description"]
    assert week.get("default") is None
    assert "week" not in described.get("required", [])
    # 不传 week 时校验通过并给出 None（即「不限教学周」）
    assert adapter.args_schema.model_validate({}).model_dump() == {"week": None}


def test_model_cannot_override_the_bound_student_id() -> None:
    """需求 3：绑定身份优先于同名入参——不存在查询他人数据的入口。"""

    client = FakeMCPClient(payload=COURSE_SCHEDULE_PAYLOAD)
    adapter = _course_adapter(client, student_id="student-001")

    adapter.invoke({"student_id": "student-002", "week": 3})

    assert client.calls == [
        (COURSE_TOOL_NAME, {"student_id": "student-001", "week": 3})
    ]


def test_bound_student_id_is_filled_in_when_the_model_omits_it() -> None:
    """需求 3：模型不需要（也无法）提供学号，适配层用配置补齐。"""

    client = FakeMCPClient(payload=COURSE_SCHEDULE_PAYLOAD)
    adapter = _course_adapter(client, student_id="student-777")

    payload = adapter.invoke({})

    assert payload == COURSE_SCHEDULE_PAYLOAD
    assert client.calls == [(COURSE_TOOL_NAME, {"student_id": "student-777"})]


# ==================================== 5 / 6 / 7. 服务端调用与参数透传


def test_course_tool_call_reaches_the_campus_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 5 / 6：工具真的经 ``CampusApiClient`` 打 ``/api/courses``，参数原样透传。"""

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests),
    )

    result = _call_course(build_server(), {"student_id": "student-001", "week": 3})

    assert len(requests) == 1, "MCP 工具必须真的经 CampusApiClient 发起了请求"
    assert requests[0].url.path == "/api/courses"
    assert dict(requests[0].url.params) == {"studentId": "student-001", "week": "3"}
    assert result.is_error is False


def test_course_tool_without_week_omits_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 4：不传 ``week`` 时请求里就不带该参数（整学期课表）。"""

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests),
    )

    result = _call_course(build_server(), {"student_id": "student-001"})

    assert dict(requests[0].url.params) == {"studentId": "student-001"}
    assert result.structured_content == {"result": COURSE_SCHEDULE_PAYLOAD}


def test_course_tool_passes_arguments_positionally_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入参没有被吞掉或改名：客户端收到的正是 ``student_id`` 与 ``week``。"""

    seen: list[tuple[str | None, int | None]] = []

    class RecordingClient:
        def fetch_course_schedule(
            self, student_id: str | None = None, week: int | None = None
        ) -> list[dict[str, str]]:
            seen.append((student_id, week))
            return list(COURSE_SCHEDULE_PAYLOAD)

    patch_campus_api_in(monkeypatch, server_module, RecordingClient())  # type: ignore[arg-type]

    _call_course(build_server(), {"student_id": "student-777", "week": 5})

    assert seen == [("student-777", 5)]


def test_course_tool_returns_contract_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 7：返回结构即对外契约——六字段字符串，且随 ``week`` 变化。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_course(build_server(), {"student_id": "student-001", "week": 8})
    rows = result.structured_content["result"]

    assert rows == course_schedule_payload(week=8)
    for row in rows:
        assert set(row) == CONTRACT_FIELDS
        assert all(isinstance(value, str) for value in row.values())


def test_course_tool_output_follows_the_service_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行为证明：换一份服务响应，MCP 工具输出随之改变。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(
            payload=[
                {"course": "量子信息导论", "teacher": "钱老师", "weekday": "周六",
                 "time": "10:00-11:40", "room": "D101", "weeks": "3-9周"}
            ]
        ),
    )

    result = _call_course(build_server(), {"student_id": "student-001"})

    assert result.structured_content == {
        "result": [
            {"course": "量子信息导论", "teacher": "钱老师", "weekday": "周六",
             "time": "10:00-11:40", "room": "D101", "weeks": "3-9周"}
        ]
    }


def test_course_empty_result_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """该学生没课返回空数组，而不是失败。"""

    patch_campus_api_in(monkeypatch, server_module, make_campus_client(payload=[]))

    result = _call_course(build_server(), {"student_id": "student-001"})

    assert result.is_error is False
    assert result.structured_content == {"result": []}


# ============================================================ 失败路径


def test_course_business_failure_is_raised_as_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务失败必须走 ``ToolError``——只有它会把原始消息交给客户端/模型。"""

    class FailingClient:
        def fetch_course_schedule(
            self, student_id: str | None = None, week: int | None = None
        ) -> list[dict[str, str]]:
            raise CampusServiceUnavailable("校园业务服务暂时不可用，请稍后再试。")

    patch_campus_api_in(monkeypatch, server_module, FailingClient())  # type: ignore[arg-type]

    with pytest.raises(ToolError) as excinfo:
        _call_course(build_server(), {"student_id": "student-001"})

    assert "校园业务服务暂时不可用" in str(excinfo.value)


def test_course_invalid_week_is_rejected_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求 8 的参数侧：``week`` 越界在服务端本地被拒，且原因可读。"""

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests),
    )

    with pytest.raises(ToolError) as excinfo:
        _call_course(build_server(), {"student_id": "student-001", "week": 99})

    assert "1-30" in str(excinfo.value)
    assert requests == [], "越界参数不该产生一次注定失败的往返请求"


def test_course_missing_required_argument_is_rejected_by_the_schema() -> None:
    """缺 ``student_id`` 由 SDK 的入参校验拦截（工具不必自己判断）。"""

    with pytest.raises(ToolError):
        _call_course(build_server(), {})


# ============================================ 适配层：共用会话、工具链可用性


def test_both_mcp_tools_share_one_client_session() -> None:
    """两个工具跑在同一条会话上：**不为课程另起第二个 Server 进程**。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions(), payload=[])
    exam = build_exam_mcp_tool(client, settings=make_mcp_settings())
    course = build_course_mcp_tool(client, settings=make_mcp_settings())

    assert isinstance(exam, MCPToolAdapter)
    assert isinstance(course, MCPToolAdapter)
    assert exam.remote_name == EXAM_TOOL_NAME
    assert course.remote_name == COURSE_TOOL_NAME

    exam.invoke({})
    course.invoke({})

    # 两次调用都落在同一个客户端对象上 = 同一条会话
    assert [name for name, _ in client.calls] == [EXAM_TOOL_NAME, COURSE_TOOL_NAME]


def test_course_mcp_tool_is_usable_by_langchain_function_calling() -> None:
    """LangChain 能把课程适配器转成函数调用 schema（``bind_tools`` 走的就是它）。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    converted = convert_to_openai_tool(tools[1])

    assert converted["function"]["name"] == COURSE_TOOL_NAME
    assert converted["function"]["description"]
    parameters = converted["function"]["parameters"]
    assert parameters["type"] == "object"
    assert "week" in parameters["properties"]
    assert "student_id" not in parameters["properties"], "身份参数不得暴露给模型"


def test_course_mcp_adapter_delegates_failures_without_swallowing_them() -> None:
    """适配层只传递失败，不吞掉（由 ``handle_tool_error`` 统一转成 ToolMessage）。"""

    client = FakeMCPClient(
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        )
    )
    adapter = _course_adapter(client)

    with pytest.raises(MCPToolInvocationError) as excinfo:
        adapter.invoke({"week": 3})

    assert "校园业务服务暂时不可用" in str(excinfo.value)


# ============================================ 装配：槽位替换与回退粒度


def test_enabled_mcp_replaces_every_slot() -> None:
    """三个查询槽位都被 MCP 适配器替换；请假提交作为第 4 个工具被**追加**。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    # 前三个槽位按名字**顶替**（不叠加），因此清单名与直连清单逐位一致
    assert [tool.name for tool in tools[:3]] == [tool.name for tool in AGENT_TOOLS]
    assert isinstance(tools[0], MCPToolAdapter) and tools[0].remote_name == EXAM_TOOL_NAME
    assert isinstance(tools[1], MCPToolAdapter) and tools[1].remote_name == COURSE_TOOL_NAME
    assert isinstance(tools[2], MCPToolAdapter) and tools[2].remote_name == "get_score"
    # 请假提交没有直连对应物（AGENT_TOOLS 只登记只读查询），只能追加在末尾。
    # 它的完整行为由 tests/mcp/test_leave_tool.py 覆盖。
    assert tools[3].name == "submit_leave_application"


def test_service_end_to_end_success() -> None:
    """端到端：Agent -> ToolNode -> MCP 适配器 -> 客户端，成功路径。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(), payload=COURSE_SCHEDULE_PAYLOAD
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        _tool_call(COURSE_TOOL_NAME, {"week": 3}), AIMessage(content="已查到第 3 周课表。")
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, COURSE_QUESTION, "t-mcp-course-ok")

    executed = _tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "success"
    assert executed[0].name == COURSE_TOOL_NAME
    assert "数据结构" in executed[0].content
    # 参数经适配层补齐学号后到达客户端
    assert client.calls == [(COURSE_TOOL_NAME, {"student_id": "student-001", "week": 3})]
    assert state["messages"][-1].content == "已查到第 3 周课表。"


def test_toolnode_turns_course_mcp_failure_into_an_error_message() -> None:
    """需求 8：MCP 侧失败被转成 error ``ToolMessage``，**整轮对话不崩**。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        ),
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        _tool_call(COURSE_TOOL_NAME, {"week": 3}),
        AIMessage(content="课表暂时查不到，请稍后再试。"),
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, COURSE_QUESTION, "t-mcp-course-fail")

    executed = _tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "校园业务服务暂时不可用" in executed[0].content
    assert "Traceback" not in executed[0].content
    # 图正常走完，模型据错误消息作答
    assert state["messages"][-1].content == "课表暂时查不到，请稍后再试。"


def test_course_slot_falls_back_alone_when_only_it_is_missing() -> None:
    """回退粒度按工具：服务端只注册考试工具时，课程回退、考试仍走 MCP。

    这条守住「MCP 少注册一个工具不该把已可用的能力一起降级」。
    """

    client = FakeMCPClient(tools=(make_mcp_tool_definition(),))
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert isinstance(tools[0], MCPToolAdapter)
    assert tools[0].remote_name == EXAM_TOOL_NAME
    assert tools[1] is AGENT_TOOLS[1]
    assert tools[2] is AGENT_TOOLS[2]


def test_course_slot_missing_is_reported_by_the_builder() -> None:
    """服务端没有课程工具时，构建函数明确抛 ``MCPToolNotFound``（不静默返回假工具）。"""

    with pytest.raises(MCPToolNotFound):
        build_course_mcp_tool(
            FakeMCPClient(tools=(make_mcp_tool_definition(),)),
            settings=make_mcp_settings(),
        )


def test_unavailable_server_falls_back_every_slot_to_direct() -> None:
    """MCP 整体不可用时**三个槽位**都回退——等价于改造前的「整体回退」。"""

    client = FakeMCPClient(
        start_error=MCPServerUnavailable("校园 MCP 服务暂时不可用，请稍后再试。")
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert not isinstance(tools[0], MCPToolAdapter)
    assert not isinstance(tools[1], MCPToolAdapter)
    assert not isinstance(tools[2], MCPToolAdapter)


def test_disabled_mcp_keeps_every_tool_direct() -> None:
    """``CAMPUS_MCP_ENABLED=false`` 时清单与既有实现**完全相同**，且不建连。"""

    client = FakeMCPClient()
    tools = build_agent_tools(settings=make_mcp_settings(enabled=False), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert client.start_count == 0, "未启用时不该有任何 MCP 建连动作"


# ============================================================ 9 / 10 / 11. 回归


def test_exam_mcp_behaviour_is_unchanged() -> None:
    """需求 9：考试槽位的发现、绑定与调用行为与改造前一致。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(), payload=EXAM_SCHEDULE_PAYLOAD
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    exam = tools[0]

    assert isinstance(exam, MCPToolAdapter)
    assert exam.remote_name == EXAM_TOOL_NAME
    assert exam.bound_arguments == {EXAM_TOOL_BOUND_ARGUMENT: "student-001"}
    # 考试工具对模型完全无参（身份被绑定），与改造前一致
    assert exam.args_schema.model_json_schema().get("properties", {}) == {}
    assert exam.invoke({}) == EXAM_SCHEDULE_PAYLOAD
    assert client.calls == [(EXAM_TOOL_NAME, {"student_id": "student-001"})]


def test_score_slot_is_now_provided_by_mcp() -> None:
    """需求 10（已随本阶段反转）：成绩槽位现在也是 MCP，不再是直连工具。

    本阶段把成绩从「普通 Tool」搬到 MCP 之后，原先「score 仍是直连」的断言
    不再成立；这里改为守住新契约——成绩槽位是 MCP 适配器，且与课程工具
    跑在**同一个** Server 会话上。成绩工具自身的完整用例见
    ``tests/mcp/test_score_tool.py``。
    """

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert tools[2] is not get_score
    assert isinstance(tools[2], MCPToolAdapter)
    assert tools[2].remote_name == "get_score"


def test_score_is_registered_on_the_mcp_server() -> None:
    """需求 10（已随本阶段反转）：MCP Server 上**有**成绩工具了。"""

    names = [tool.name for tool in _list_tools(build_server())]
    assert "get_score" in names

    assert FakeMCPClient(tools=make_mcp_server_tool_definitions()).find_tool("get_score")


def test_course_and_score_mcp_tools_work_in_the_same_batch() -> None:
    """同一批次里两个 MCP 工具可以混合执行，各自拿到各自结构的返回。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={
            COURSE_TOOL_NAME: COURSE_SCHEDULE_PAYLOAD,
            "get_score": SCORE_PAYLOAD,
        },
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        AIMessage(
            content="",
            tool_calls=[
                {"name": COURSE_TOOL_NAME, "args": {"week": 3},
                 "id": "call-1", "type": "tool_call"},
                {"name": "get_score", "args": {"course": "数据结构"},
                 "id": "call-2", "type": "tool_call"},
            ],
        ),
        AIMessage(content="课表与成绩都查到了。"),
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, "我这周有什么课？数据结构考了多少分？", "t-mcp-batch")

    by_call_id = {m.tool_call_id: m for m in _tool_messages(state)}
    assert by_call_id["call-1"].status == "success"
    assert by_call_id["call-2"].status == "success"
    # 两个工具各拿各的（六字段课表 / 四字段成绩），互不串扰
    assert "数据结构" in by_call_id["call-1"].content
    assert "A302" in by_call_id["call-1"].content
    assert '"score": 92.0' in by_call_id["call-2"].content
    assert '"semester": "2025-2026-1"' in by_call_id["call-2"].content
    # 两次调用都落在同一个客户端对象上 = 同一条会话
    assert [name for name, _ in client.calls] == [COURSE_TOOL_NAME, "get_score"]


def test_knowledge_branch_does_not_touch_mcp() -> None:
    """需求 11：RAG 分支既不碰模型也不碰 MCP（一次调用都没有）。"""

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

    state = _invoke(graph, "图书馆开放时间是什么？", "t-mcp-rag")

    assert state["messages"][-1].content == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert rag.calls, "knowledge 分支应当走 RAG"
    assert client.calls == [], "RAG 分支不得触发任何 MCP 工具调用"
    assert client.start_count == starts_before
    assert _tool_messages(state) == []


def test_chat_branch_does_not_touch_mcp() -> None:
    """需求 11：chat 分支不绑工具、不碰 MCP。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    graph = _build_test_graph(
        tools,
        ScriptedChatModel.scripted(AIMessage(content="你好！有什么可以帮你？")),
        intent="chat",
    )

    state = _invoke(graph, "你好", "t-mcp-chat")

    assert state["messages"][-1].content == "你好！有什么可以帮你？"
    assert client.calls == []
    assert _tool_messages(state) == []


def test_intent_routing_is_unchanged() -> None:
    """需求 11：意图到节点的映射未变（query/action -> agent，knowledge -> knowledge）。"""

    assert route_by_intent({"intent": "query"}) == AGENT_NODE
    assert route_by_intent({"intent": "knowledge"}) == KNOWLEDGE_NODE


# ============================================================ 结构守卫


def test_mcp_server_source_has_no_course_data_literals() -> None:
    """课程数据只在 MySQL 里：MCP Server 源码不得出现课程数据字面量。"""

    source = Path(server_module.__file__).read_text(encoding="utf-8")

    for literal in COURSE_DATA_LITERALS:
        assert literal not in source, f"MCP Server 出现业务数据字面量 {literal!r}"


def test_mcp_adapter_source_has_no_course_data_literals() -> None:
    """适配层同理：只做协议转换，不携带业务数据。"""

    source = Path(tools_module.__file__).read_text(encoding="utf-8")

    for literal in COURSE_DATA_LITERALS:
        assert literal not in source, f"适配层出现业务数据字面量 {literal!r}"
