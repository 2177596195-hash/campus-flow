"""MCP -> LangChain 适配层与工具清单解析用例（完全离线）。

这一层最容易出的问题不是「跑不起来」，而是**悄悄跑偏**：

- 适配器把 description / args_schema 自己重写一份 → 与服务端的工具说明书漂移；
- 绑定参数（学号）泄漏进模型可见的 schema → 模型去猜别人的学号；
- MCP 与直连工具**同时**留在清单里 → 模型看到两个同名能力；
- MCP 挂掉后悄悄回退，让人误以为在走 MCP。

因此下面的用例既断言正常行为，也断言这些失败形态不会发生。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agent.nodes.tool_agent import AGENT_TOOLS
from app.mcp import tools as tools_module
from app.mcp.client import MCPServerUnavailable, MCPToolInvocationError, MCPToolNotFound
from app.mcp.tools import (
    EXAM_TOOL_BOUND_ARGUMENT,
    MCPToolAdapter,
    build_agent_tools,
    build_args_schema,
    build_exam_mcp_tool,
    make_mcp_tool,
)
from tests._fakes import (
    EXAM_SCHEDULE_PAYLOAD,
    FakeMCPClient,
    make_mcp_settings,
    make_mcp_tool_definition,
)

MULTI_ARG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "student_id": {"type": "string", "description": "学号"},
        "limit": {"type": "integer", "default": 5, "description": "返回条数"},
        "verbose": {"type": "boolean", "description": "是否详细"},
    },
    "required": ["student_id"],
}


# ============================================================ schema 生成


def _json_type(field: dict[str, Any]) -> str:
    """取出字段的 JSON 类型。

    可带默认值的参数在 pydantic 里是 ``Optional[...]``，其 schema 会变成
    ``{"anyOf": [{"type": "integer"}, {"type": "null"}]}``，类型不在顶层 ``type`` 上。
    """

    if "type" in field:
        return field["type"]
    return next(alt["type"] for alt in field.get("anyOf", []) if alt.get("type") != "null")


def test_args_schema_is_derived_from_the_server_schema() -> None:
    """``args_schema`` 由服务端 ``inputSchema`` 生成，字段名与类型都对得上。"""

    schema = build_args_schema(make_mcp_tool_definition(input_schema=MULTI_ARG_SCHEMA))
    described = schema.model_json_schema()
    properties = described["properties"]

    assert set(properties) == {"student_id", "limit", "verbose"}
    assert _json_type(properties["student_id"]) == "string"
    assert _json_type(properties["limit"]) == "integer"
    assert _json_type(properties["verbose"]) == "boolean"
    assert set(described.get("required", [])) == {"student_id"}


def test_required_argument_has_no_default_and_optional_keeps_it() -> None:
    """必填参数不带默认值，可选参数沿用服务端给的默认值。"""

    schema = build_args_schema(make_mcp_tool_definition(input_schema=MULTI_ARG_SCHEMA))
    instance = schema.model_validate({"student_id": "student-001"})

    assert instance.model_dump() == {"student_id": "student-001", "limit": 5, "verbose": None}


def test_binding_drops_the_argument_from_the_model_facing_schema() -> None:
    """被宿主绑定的参数**不暴露给模型**（学号由身份配置决定，不该让模型填）。"""

    schema = build_args_schema(
        make_mcp_tool_definition(input_schema=MULTI_ARG_SCHEMA),
        bound_arguments={EXAM_TOOL_BOUND_ARGUMENT: "student-001"},
    )

    assert set(schema.model_json_schema().get("properties", {})) == {"limit", "verbose"}


def test_all_arguments_bound_yields_an_empty_schema() -> None:
    """本阶段的实际形态：唯一参数被绑定后，模型看到的是一个无参工具。"""

    schema = build_args_schema(
        make_mcp_tool_definition(),
        bound_arguments={EXAM_TOOL_BOUND_ARGUMENT: "student-001"},
    )

    assert schema.model_json_schema()["properties"] == {}


def test_unknown_json_type_falls_back_to_any() -> None:
    """没登记的类型退化为 ``Any``（不猜、也不因此报错）。"""

    schema = build_args_schema(
        make_mcp_tool_definition(
            input_schema={"type": "object", "properties": {"weird": {"type": "geopoint"}}}
        )
    )

    assert "weird" in schema.model_json_schema()["properties"]


# ============================================================ 适配器身份与调用


def test_adapter_identity_comes_from_discovery() -> None:
    """``name`` / ``description`` 来自 ``tools/list`` 的发现结果，不是本地重写。"""

    definition = make_mcp_tool_definition(
        name="get_exam_schedule", description="服务端给的描述"
    )
    adapter = make_mcp_tool(
        FakeMCPClient(tools=[definition], payload=EXAM_SCHEDULE_PAYLOAD), definition
    )

    assert adapter.name == "get_exam_schedule"
    assert adapter.description == "服务端给的描述"
    assert adapter.remote_name == "get_exam_schedule"


def test_run_merges_bound_arguments_into_the_call() -> None:
    """``_run`` 把绑定参数补齐后交给 MCP Client（工具本身不碰业务逻辑）。"""

    client = FakeMCPClient(payload=EXAM_SCHEDULE_PAYLOAD)
    definition = make_mcp_tool_definition()
    adapter = make_mcp_tool(
        client, definition, bound_arguments={EXAM_TOOL_BOUND_ARGUMENT: "student-001"}
    )

    payload = adapter.invoke({})

    assert payload == EXAM_SCHEDULE_PAYLOAD
    assert client.calls == [("get_exam_schedule", {"student_id": "student-001"})]


def test_bound_arguments_cannot_be_overridden_by_the_call() -> None:
    """绑定参数优先于同名入参：学号不该存在被覆盖的入口。

    这条守的是越权：若绑定身份能被入参改掉，一个「对模型隐藏」的参数
    反而成了查询他人数据的后门。模型能填的参数不受影响。
    """

    client = FakeMCPClient()
    definition = make_mcp_tool_definition()
    adapter = make_mcp_tool(
        client, definition, bound_arguments={EXAM_TOOL_BOUND_ARGUMENT: "student-001"}
    )

    merged = adapter.merged_arguments({EXAM_TOOL_BOUND_ARGUMENT: "student-002", "limit": 3})

    assert merged == {EXAM_TOOL_BOUND_ARGUMENT: "student-001", "limit": 3}

    adapter.invoke({EXAM_TOOL_BOUND_ARGUMENT: "student-002"})
    assert client.calls == [("get_exam_schedule", {"student_id": "student-001"})]


def test_adapter_is_usable_by_langchain_function_calling() -> None:
    """LangChain 能把适配器转成函数调用 schema（``bind_tools`` 走的就是它）。

    自定义 ``BaseTool`` 子类若漏了 ``args_schema`` 或类型不对，会在这一步暴露，
    而不是等到真实模型调用时才报错。
    """

    definition = make_mcp_tool_definition()
    adapter = make_mcp_tool(
        FakeMCPClient(), definition, bound_arguments={EXAM_TOOL_BOUND_ARGUMENT: "student-001"}
    )

    converted = convert_to_openai_tool(adapter)

    assert converted["function"]["name"] == "get_exam_schedule"
    assert converted["function"]["description"]
    assert converted["function"]["parameters"]["type"] == "object"


def test_adapter_delegates_failures_without_swallowing_them() -> None:
    """MCP 侧失败原样上抛（由 ``handle_tool_error`` 统一转成 ToolMessage）。"""

    client = FakeMCPClient(
        call_error=MCPServerUnavailable("校园 MCP 服务暂时不可用，请稍后再试。")
    )
    definition = make_mcp_tool_definition()
    adapter = make_mcp_tool(client, definition)

    with pytest.raises(MCPServerUnavailable):
        adapter.invoke({"student_id": "student-001"})


def test_adapter_does_not_contain_business_data_literals() -> None:
    """适配层不得出现业务数据字面量（数据只在 MySQL 里）。"""

    source = Path(tools_module.__file__).read_text(encoding="utf-8")
    for literal in ("计算机网络", "数据结构", "A302", "2026-09-28"):
        assert literal not in source, f"适配层出现业务数据字面量 {literal!r}"


# ============================================================ 工具清单解析


def test_build_exam_tool_starts_the_session_and_discovers() -> None:
    """解析工具时确实建连并做了工具发现（而不是凭空造一个工具对象）。"""

    client = FakeMCPClient()

    adapter = build_exam_mcp_tool(client, settings=make_mcp_settings())

    assert client.start_count == 1
    assert isinstance(adapter, MCPToolAdapter)
    assert adapter.remote_tool.name == "get_exam_schedule"


def test_disabled_mcp_keeps_the_direct_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CAMPUS_MCP_ENABLED=false`` 时工具清单与既有实现**完全相同**。"""

    client = FakeMCPClient()

    tools = build_agent_tools(settings=make_mcp_settings(enabled=False), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert client.start_count == 0, "未启用时不该有任何 MCP 建连动作"


def test_enabled_mcp_replaces_only_the_exam_slot() -> None:
    """考试能力**只有一个槽位**：被 MCP 适配器替换，另两项原样保留。

    这条用例守的是最容易犯的错——「把 MCP 工具追加进清单」，
    那会让模型看到两个同名考试工具。
    """

    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=FakeMCPClient())

    assert [tool.name for tool in tools] == [tool.name for tool in AGENT_TOOLS]
    assert isinstance(tools[0], MCPToolAdapter)
    assert tools[1] is AGENT_TOOLS[1]
    assert tools[2] is AGENT_TOOLS[2]
    assert len({tool.name for tool in tools}) == len(tools)


def test_unavailable_mcp_falls_back_to_the_direct_tool() -> None:
    """MCP 拉不起来时回退直连：Agent 仍能查考试，但不会静默冒充 MCP。"""

    client = FakeMCPClient(
        start_error=MCPServerUnavailable("校园 MCP 服务暂时不可用，请稍后再试。")
    )

    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)

    assert tools == tuple(AGENT_TOOLS)
    assert not isinstance(tools[0], MCPToolAdapter)


def test_missing_tool_on_the_server_also_falls_back() -> None:
    """服务端没注册该工具时同样回退（而不是留一个必然失败的工具）。"""

    client = FakeMCPClient(tools=[])

    with pytest.raises(MCPToolNotFound):
        build_exam_mcp_tool(client, settings=make_mcp_settings())

    tools = build_agent_tools(settings=make_mcp_settings(enabled=True), client=client)
    assert tools == tuple(AGENT_TOOLS)


def test_business_failure_inside_the_server_propagates_through_the_adapter() -> None:
    """业务失败经适配器上抛，类型与文案都不被抹平。

    Server 端失败在 Client 里归为 ``MCPToolInvocationError``（服务端明确返回
    ``isError``）；适配层只负责传递，不做「统一包一层」——否则节点侧就再也
    分不清「MCP 挂了」和「业务侧没有这条数据」。
    """

    client = FakeMCPClient(
        call_error=MCPToolInvocationError(
            "校园 MCP 服务执行工具失败: 校园业务服务暂时不可用，请稍后再试。"
        )
    )
    definition = make_mcp_tool_definition()
    adapter = make_mcp_tool(client, definition)

    with pytest.raises(MCPToolInvocationError) as excinfo:
        adapter.invoke({"student_id": "student-001"})

    assert "校园业务服务暂时不可用" in str(excinfo.value)
