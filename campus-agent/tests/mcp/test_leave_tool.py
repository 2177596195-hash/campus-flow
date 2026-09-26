"""请假提交经 Campus MCP Server 暴露的测试（完全离线）。

这是全项目**唯一一个写操作**，链路与三个查询工具一致::

    Agent -> MCP Client -> Campus MCP Server -> CampusApiClient -> Spring Boot -> MySQL

但写操作多出两类必须守住的边界，本文件按类覆盖：

======================  ==========================================================
类别                     对应用例
======================  ==========================================================
1 服务端注册与说明        ``test_server_exposes_the_leave_tool`` /
                         ``test_leave_tool_schema_requires_the_three_user_fields`` /
                         ``test_description_draws_the_line_between_submitting_and_asking``
2 服务端取数与翻译        ``test_tool_call_posts_the_normalised_body`` /
                         ``test_tool_returns_the_contract_record`` /
                         ``test_invalid_arguments_become_tool_errors`` /
                         ``test_business_failure_is_raised_as_tool_error``
3 适配层身份与参数        ``test_leave_mcp_tool_binds_the_student_id`` /
                         ``test_model_facing_schema_is_the_three_leave_fields`` /
                         ``test_model_cannot_override_the_bound_student_id`` /
                         ``test_adapter_delegates_failures``
4 装配与端到端            ``test_enabled_mcp_appends_the_leave_tool`` /
                         ``test_disabled_mcp_has_no_leave_tool`` /
                         ``test_missing_leave_tool_on_the_server_is_skipped`` /
                         ``test_end_to_end_submission_through_the_graph`` /
                         ``test_toolnode_turns_leave_mcp_failure_into_an_error_message``
======================  ==========================================================

手法与既有测试一致：**只替换「数据从哪来」这一处**。
- 服务端用例把 ``app.mcp.server`` 命名空间里的 ``get_campus_api_client`` 换成挂在
  ``httpx.MockTransport`` 上的替身（它会**回显请求体**，因此「参数到底有没有传下去」
  是可精确断言的事实），Server 自身的注册、schema、参数归一化、结果整理、
  异常翻译全部真实执行；
- 适配层与装配用例用 ``FakeMCPClient``（与 ``StdioMCPClient`` 同名同签名），
  不起子进程；
- 真实 stdio 协议往返（含四个工具的发现）在 ``tests/mcp/test_client_stdio.py``。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.server.mcpserver.exceptions import ToolError

from app.agent.nodes.tool_agent import AGENT_TOOLS
from app.infrastructure.campus_api import CampusServiceUnavailable
from app.mcp import server as server_module
from app.mcp import tools as tools_module
from app.mcp.client import MCPServerUnavailable, MCPToolInvocationError, MCPToolNotFound
from app.mcp.server import TOOL_SUBMIT_LEAVE_APPLICATION, build_server
from app.mcp.tools import (
    LEAVE_TOOL_BOUND_ARGUMENT,
    LEAVE_TOOL_NAME,
    MCPToolAdapter,
    build_agent_tools,
    build_leave_mcp_tool,
)
from app.tools.leave_contract import LEAVE_RESULT_FIELDS
from tests._fakes import (
    DEFAULT_CAMPUS_PAYLOADS,
    LEAVE_APPLICATION_PAYLOAD,
    FakeMCPClient,
    ScriptedChatModel,
    make_campus_client,
    make_mcp_leave_tool_definition,
    make_mcp_server_tool_definitions,
    make_mcp_settings,
    make_mcp_tool_definition,
    patch_campus_api_in,
)

STUDENT = "student-001"

#: 工具入参（用户侧那三个字段），全部是有意选取的中性示例
START = "2026-10-12 08:00"
END = "2026-10-14 18:00"
REASON = "感冒发烧需就医"

# --------------------------------------------------------------- 测试用构造


def _list_tools(server: Any) -> list[Any]:
    return list(asyncio.run(server.list_tools()))


def _tool_named(server: Any, name: str) -> Any:
    for tool in _list_tools(server):
        if tool.name == name:
            return tool
    raise AssertionError(f"Server 未注册工具 {name}")


def _call_leave(server: Any, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(TOOL_SUBMIT_LEAVE_APPLICATION, arguments))


def _full_arguments() -> dict[str, Any]:
    return {
        "student_id": STUDENT,
        "start_time": START,
        "end_time": END,
        "reason": REASON,
    }


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _build_test_graph(tools: Any, llm: Any, intent: str = "action"):
    from app.agent.graph import build_graph
    from tests._fakes import FakeRAGService, StaticIntentClassifier

    return build_graph(
        llm=llm,
        intent_classifier=StaticIntentClassifier(intent),  # type: ignore[arg-type]
        rag_service=FakeRAGService(),  # type: ignore[arg-type]
        tools=tools,
    )


def _invoke(graph: Any, text: str, thread_id: str = "t-leave") -> dict[str, Any]:
    return graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def _tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [message for message in state["messages"] if isinstance(message, ToolMessage)]


# ============================================ 1. 服务端：注册、schema、说明


def test_server_exposes_the_leave_tool() -> None:
    """``tools/list`` 里有 ``submit_leave_application``，且与适配层的名字一致。"""

    assert LEAVE_TOOL_NAME == TOOL_SUBMIT_LEAVE_APPLICATION == "submit_leave_application"

    names = [tool.name for tool in _list_tools(build_server())]

    assert TOOL_SUBMIT_LEAVE_APPLICATION in names


def test_leave_tool_schema_requires_the_three_user_fields() -> None:
    """三个用户字段必填、``student_id`` 也必填——身份由宿主补齐，不由模型省略。"""

    tool = _tool_named(build_server(), TOOL_SUBMIT_LEAVE_APPLICATION)

    schema = tool.input_schema
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"student_id", "start_time", "end_time", "reason"}
    assert set(schema["required"]) == {"student_id", "start_time", "end_time", "reason"}
    # 每个参数都要有说明：适配层会把 inputSchema 当唯一事实来源
    for name in ("start_time", "end_time", "reason"):
        assert schema["properties"][name]["description"], f"{name} 缺少参数说明"


def test_description_draws_the_line_between_submitting_and_asking() -> None:
    """写工具的描述必须说清「提交，不是查询 / 审批」。

    模型若把「我的请假批了吗」当成提交意图，就会凭空造出一条申请——
    查询答错可以纠正，写错就是一条脏数据。
    """

    description = _tool_named(build_server(), TOOL_SUBMIT_LEAVE_APPLICATION).description or ""

    assert "提交" in description
    assert "不能查询" in description
    for keyword in ("id", "status", "待审核"):
        assert keyword in description, f"描述里应告知返回的 {keyword}"


# ============================================ 2. 服务端：取数与失败翻译


def test_tool_call_posts_the_normalised_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """参数被归一化后进请求体，且**真的**经由 CampusApiClient 发出 POST。"""

    requests: list[Any] = []
    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(
            status_code=201, payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests
        ),
    )

    _call_leave(build_server(), _full_arguments())

    assert len(requests) == 1, "MCP 工具必须真的经由 CampusApiClient 提交"
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/leaves"
    body = json.loads(request.content.decode("utf-8"))
    # 归一化后的形态（分钟精度 ISO 8601），不是模型给的空格写法
    assert body == {
        "studentId": STUDENT,
        "startTime": "2026-10-12T08:00",
        "endTime": "2026-10-14T18:00",
        "reason": REASON,
    }


def test_tool_returns_the_contract_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回结构符合对外契约：七个字段、值均为字符串。

    注：MCP SDK 对 ``dict`` 返回值**不**包 ``{"result": ...}``（那是 list 返回值的
    形态），structured_content 即字典本身——这里同时守住这条 SDK 行为，
    免得有人照着考试工具的断言「修」出一份错误预期。
    """

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(status_code=201, payloads=DEFAULT_CAMPUS_PAYLOADS),
    )

    result = _call_leave(build_server(), _full_arguments())

    assert result.is_error is False
    record = result.structured_content
    assert tuple(record) == LEAVE_RESULT_FIELDS
    assert all(isinstance(value, str) for value in record.values())
    assert record["status"] == "PENDING"
    assert record["studentId"] == STUDENT


def test_service_response_shape_drives_the_tool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一份服务响应，工具输出随之改变（不是写死的记录）。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(
            status_code=201,
            payload={
                "id": 42,
                "studentId": STUDENT,
                "startTime": "2027-01-05T08:30",
                "endTime": "2027-01-05T12:00",
                "reason": "就医",
                "status": "PENDING",
                "createdAt": "2027-01-04T20:00",
            },
        ),
    )

    result = _call_leave(build_server(), _full_arguments())

    record = result.structured_content
    assert record["id"] == "42"
    assert record["startTime"] == "2027-01-05T08:30"
    assert record["reason"] == "就医"


@pytest.mark.parametrize(
    ("overrides", "expected_token"),
    [
        ({"start_time": "明天早上"}, "格式无法识别"),
        ({"end_time": "2026/10/14 18:00"}, "格式无法识别"),
        ({"start_time": ""}, "不能为空"),
        ({"reason": "   "}, "事由"),
    ],
)
def test_invalid_arguments_become_tool_errors(
    overrides: dict[str, str], expected_token: str
) -> None:
    """入参不合约定走 ``ToolError``，模型据此改参重试（而不是发出半成品请求）。"""

    arguments = {**_full_arguments(), **overrides}

    with pytest.raises(ToolError) as excinfo:
        _call_leave(build_server(), arguments)

    assert expected_token in str(excinfo.value)


def test_missing_required_argument_is_rejected_by_the_schema() -> None:
    """缺参数由 SDK 的入参校验拦截（工具不必自己判断）。"""

    with pytest.raises(ToolError):
        _call_leave(build_server(), {"student_id": STUDENT})


def test_business_failure_is_raised_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务失败必须是 ``ToolError``：只有它会把原始消息交给模型。

    写操作尤其依赖这条——模型必须看到「服务暂时不可用」才能如实告知用户，
    而不是以为申请已经提交成功。
    """

    class FailingClient:
        def submit_leave_application(self, **kwargs: Any) -> dict[str, Any]:
            raise CampusServiceUnavailable("校园业务服务暂时不可用，请稍后再试。")

    patch_campus_api_in(monkeypatch, server_module, FailingClient())  # type: ignore[arg-type]

    with pytest.raises(ToolError) as excinfo:
        _call_leave(build_server(), _full_arguments())

    assert "校园业务服务暂时不可用" in str(excinfo.value)


def test_rejected_submission_surfaces_the_server_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端 4xx 的原因被原样带上（写入被拒时用户最需要知道「该改哪一项」）。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(
            status_code=400, payload={"message": "请假结束时间不能早于开始时间。"}
        ),
    )

    with pytest.raises(ToolError) as excinfo:
        _call_leave(build_server(), _full_arguments())

    assert "请假结束时间不能早于开始时间" in str(excinfo.value)


# ============================================ 3. 适配层：身份与参数


def test_leave_mcp_tool_binds_the_student_id() -> None:
    """``build_leave_mcp_tool`` 会建连、做工具发现，并把学号绑成宿主参数。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())

    adapter = build_leave_mcp_tool(client, settings=make_mcp_settings())

    assert client.start_count == 1
    assert isinstance(adapter, MCPToolAdapter)
    assert adapter.remote_name == LEAVE_TOOL_NAME
    assert adapter.bound_arguments == {LEAVE_TOOL_BOUND_ARGUMENT: STUDENT}


def test_model_facing_schema_is_the_three_leave_fields() -> None:
    """模型只能看到三个用户字段；身份参数被摘掉，且它看不到「学号」这件事。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    adapter = build_leave_mcp_tool(client, settings=make_mcp_settings())

    converted = convert_to_openai_tool(adapter)

    assert converted["function"]["name"] == LEAVE_TOOL_NAME
    assert converted["function"]["description"]
    parameters = converted["function"]["parameters"]
    assert set(parameters["properties"]) == {"start_time", "end_time", "reason"}
    assert "student_id" not in parameters["properties"], "身份参数不得暴露给模型"
    # 三个字段必填：模型看到信息不全时会先追问，而不是提交缺字段的申请
    assert set(parameters.get("required", [])) == {"start_time", "end_time", "reason"}


def test_model_cannot_override_the_bound_student_id() -> None:
    """绑定参数优先于同名入参：不存在「替别人提交请假申请」的入口。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={LEAVE_TOOL_NAME: LEAVE_APPLICATION_PAYLOAD},
    )
    adapter = build_leave_mcp_tool(client, settings=make_mcp_settings())

    arguments = {"start_time": START, "end_time": END, "reason": REASON}
    merged = adapter.merged_arguments(
        {LEAVE_TOOL_BOUND_ARGUMENT: "student-002", **arguments}
    )
    assert merged[LEAVE_TOOL_BOUND_ARGUMENT] == STUDENT

    adapter.invoke({LEAVE_TOOL_BOUND_ARGUMENT: "student-002", **arguments})
    assert client.calls == [
        (
            LEAVE_TOOL_NAME,
            {"student_id": STUDENT, **arguments},
        )
    ]


def test_adapter_delegates_failures() -> None:
    """适配层只传递失败，不吞掉（由 ``handle_tool_error`` 统一转成 ToolMessage）。"""

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        ),
    )
    adapter = build_leave_mcp_tool(client, settings=make_mcp_settings())

    with pytest.raises(MCPToolInvocationError) as excinfo:
        adapter.invoke({"start_time": START, "end_time": END, "reason": REASON})

    assert "校园业务服务暂时不可用" in str(excinfo.value)


# ============================================ 4. 装配与端到端


def test_enabled_mcp_appends_the_leave_tool() -> None:
    """请假提交没有同名直连工具，因此是**追加**到清单末尾，而不是顶替。"""

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert [tool.name for tool in tools[:3]] == [tool.name for tool in AGENT_TOOLS]
    assert [tool.name for tool in tools][3:] == [LEAVE_TOOL_NAME]
    assert isinstance(tools[3], MCPToolAdapter)


def test_disabled_mcp_has_no_leave_tool() -> None:
    """``CAMPUS_MCP_ENABLED=false`` 时清单与既有实现完全相同：写能力缺席。

    这是刻意的：请假只在 MCP 上暴露，关掉 MCP 就等于没有写能力，
    而不是偷偷回到某个直连通道（那会让「写操作只经 MCP」这条边界失效）。
    """

    client = FakeMCPClient(tools=make_mcp_server_tool_definitions())
    tools = build_agent_tools(settings=make_mcp_settings(enabled=False), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert LEAVE_TOOL_NAME not in {tool.name for tool in tools}
    assert client.start_count == 0


def test_missing_leave_tool_on_the_server_is_skipped() -> None:
    """服务端没注册该工具时不追加，且**不影响**三个查询槽位的回退判断。"""

    client = FakeMCPClient(tools=(make_mcp_tool_definition(),))

    with pytest.raises(MCPToolNotFound):
        build_leave_mcp_tool(client, settings=make_mcp_settings())

    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    # 只有考试槽位走 MCP；课程与成绩按「服务端没提供」回退直连
    assert [tool.name for tool in tools] == [tool.name for tool in AGENT_TOOLS]
    assert isinstance(tools[0], MCPToolAdapter)
    assert tools[1] is AGENT_TOOLS[1]
    assert tools[2] is AGENT_TOOLS[2]
    assert LEAVE_TOOL_NAME not in {tool.name for tool in tools}


def test_end_to_end_submission_through_the_graph() -> None:
    """端到端：Agent -> ToolNode -> MCP 适配器 -> 客户端（成功路径）。

    用户这句话刻意**带全了**起止时间与事由：信息完整的请假请求不会被
    ``task_slot`` 拦下追问（拦截路径由 ``tests/agent/test_leave_flow.py`` 覆盖），
    因此一轮之内就能走完提交。
    """

    complete_request = "我要请假，10月12日 08:00 到 10月14日 18:00，感冒发烧需就医"

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        payloads={LEAVE_TOOL_NAME: LEAVE_APPLICATION_PAYLOAD},
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        _tool_call(
            LEAVE_TOOL_NAME,
            {"start_time": START, "end_time": END, "reason": REASON},
        ),
        AIMessage(content="请假申请已提交，申请编号为 1，当前状态为待审核。"),
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, complete_request)

    executed = _tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "success"
    assert executed[0].name == LEAVE_TOOL_NAME
    assert '"status": "PENDING"' in executed[0].content
    # 适配层补齐学号后到达客户端；三个用户参数原样送达
    assert client.calls == [
        (
            LEAVE_TOOL_NAME,
            {
                "student_id": STUDENT,
                "start_time": START,
                "end_time": END,
                "reason": REASON,
            },
        )
    ]
    assert state["messages"][-1].content == "请假申请已提交，申请编号为 1，当前状态为待审核。"


def test_toolnode_turns_leave_mcp_failure_into_an_error_message() -> None:
    """MCP 侧失败被转成 error ``ToolMessage``，**整轮对话不崩**。"""

    complete_request = "我要请假，10月12日 08:00 到 10月14日 18:00，感冒发烧需就医"

    client = FakeMCPClient(
        tools=make_mcp_server_tool_definitions(),
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        ),
    )
    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    llm = ScriptedChatModel.scripted(
        _tool_call(LEAVE_TOOL_NAME, {"start_time": START, "end_time": END, "reason": REASON}),
        AIMessage(content="提交暂时没成功，请稍后再试。"),
    )
    graph = _build_test_graph(tools, llm)

    state = _invoke(graph, complete_request, "t-leave-fail")

    executed = _tool_messages(state)
    assert len(executed) == 1
    assert executed[0].status == "error"
    assert "校园业务服务暂时不可用" in executed[0].content
    assert "Traceback" not in executed[0].content
    assert state["messages"][-1].content == "提交暂时没成功，请稍后再试。"


def test_unavailable_server_keeps_every_query_slot_direct() -> None:
    """MCP 整体不可用时三个查询槽位回退直连，请假能力缺席。"""

    client = FakeMCPClient(
        start_error=MCPServerUnavailable("校园 MCP 服务暂时不可用，请稍后再试。")
    )

    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert all(not isinstance(tool, MCPToolAdapter) for tool in tools)


# ============================================ 结构守卫（禁止项）


def test_server_reuses_the_contract_instead_of_reimplementing_it() -> None:
    """Server 只做「归一化 -> 取数 -> 归一化」，不重复实现契约里的任何一步。

    请假**没有** MySQL 种子数据（追加型表不该被预置），因此这里守的不是
    「有没有泄漏某条记录」，而是三条结构性边界：归一化必须复用契约、
    初始状态不得写死在 Server 里（它由数据库默认值给）、请求路径不得自己拼。
    """

    source = Path(server_module.__file__).read_text(encoding="utf-8")

    # 参数与结果的整理都复用 app.tools.leave_contract，Server 里不另写一套
    assert "normalize_datetime" in source
    assert "normalize_required_text" in source
    assert "normalize_leave_result" in source
    # 状态由数据库默认值给，Server 不预设、不覆盖
    assert '"PENDING"' not in source
    # 不自己拼请求路径：/api/* 只属于基础设施层（CampusApiClient）
    assert "/api/leaves" not in source


def test_adapter_contains_no_business_logic() -> None:
    """适配层只做「合并参数 + 调 MCP Client」：不碰状态值、路径与 SQL。"""

    source = Path(tools_module.__file__).read_text(encoding="utf-8")

    for token in ("PENDING", "/api/", "SELECT ", "INSERT "):
        assert token not in source, f"适配层出现业务逻辑痕迹 {token!r}"
