"""Campus MCP Server 用例（**进程内**，不起子进程、不联网）。

手法与既有测试一致：只替换「数据从哪来」这一处——把 ``app.mcp.server`` 命名空间
里的 ``get_campus_api_client`` 换成挂在 ``httpx.MockTransport`` 上的替身，
Server 自身的注册、schema 生成、参数绑定、结果整理、异常翻译全部真实执行。

需要「真的起一个 MCP Server 子进程、走 stdio 协议」的用例在
``tests/mcp/test_client_stdio.py``，两者互补：

- 本文件证明**服务端逻辑**正确（schema 与返回结构、失败翻译）；
- 那个文件证明**协议往返**正确（连接、握手、工具发现、调用）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.infrastructure.campus_api import CampusServiceUnavailable
from app.mcp import server as server_module
from app.mcp.server import (
    SERVER_NAME,
    TOOL_GET_COURSE_SCHEDULE,
    TOOL_GET_EXAM_SCHEDULE,
    TOOL_GET_SCORE,
    TOOL_SUBMIT_LEAVE_APPLICATION,
    build_server,
)
from tests._fakes import EXAM_SCHEDULE_PAYLOAD, make_campus_client, patch_campus_api_in

#: 本文件聚焦**考试**工具的服务端行为；课程工具的用例在
#: ``tests/mcp/test_course_tool.py``，成绩工具的用例在 ``tests/mcp/test_score_tool.py``，
#: 请假提交（唯一的写操作）的用例在 ``tests/mcp/test_leave_tool.py``。
#: 这里只保留一条「四个工具都在同一台 Server 上」的总览断言，
#: 用于守住「不为某项能力另起一个 Server」。
EXPECTED_MCP_TOOL_NAMES: tuple[str, ...] = (
    TOOL_GET_EXAM_SCHEDULE,
    TOOL_GET_COURSE_SCHEDULE,
    TOOL_GET_SCORE,
    TOOL_SUBMIT_LEAVE_APPLICATION,
)


def _list_tools(server: Any) -> list[Any]:
    """按 SDK 方式列出已注册工具（无需活的会话）。"""

    return list(asyncio.run(server.list_tools()))


def _tool_named(server: Any, name: str) -> Any:
    """按名字取工具定义。

    注册多个工具后**不能再靠位置解包**（``(tool,) = _list_tools(...)``）——
    那样第二个工具一注册，所有旧用例就会集体报「too many values to unpack」，
    失败原因还与被测行为无关。
    """

    for tool in _list_tools(server):
        if tool.name == name:
            return tool
    raise AssertionError(f"Server 未注册工具 {name}")


def _call_tool(server: Any, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(TOOL_GET_EXAM_SCHEDULE, arguments))


def _source_of(module: Any) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


# ============================================================ 1. Server 可以启动


def test_server_can_be_built_with_stdio_entry_point() -> None:
    """Server 可构建，且提供 stdio 运行入口（子进程就是靠它被拉起的）。"""

    server = build_server()

    assert SERVER_NAME == "campus-mcp-server"
    assert callable(server.run)
    # 工具列表能列出来即说明服务器对象是可用的（注册表已就绪）
    assert len(_list_tools(server)) == len(EXPECTED_MCP_TOOL_NAMES)


# ================================================ 2. Tool 列表包含 get_exam_schedule


def test_tool_list_contains_exam_tool() -> None:
    """``tools/list`` 里能找到考试工具，且名字与 Agent 侧一致。

    本用例只断言「**包含**考试工具」——**精确的工具集合**由
    ``test_all_tools_share_one_server`` 守住。
    这样「再新增一门能力」时不必回头改这条考试用例，
    它的失败就总能指向「考试工具真的没了」而不是「工具数量变了」。
    """

    names = [tool.name for tool in _list_tools(build_server())]

    assert TOOL_GET_EXAM_SCHEDULE in names


def test_all_tools_share_one_server() -> None:
    """四个工具（三个查询 + 一个写操作）在**同一台** Server 上被暴露（不为某项另起 Server）。"""

    names = [tool.name for tool in _list_tools(build_server())]

    assert names == list(EXPECTED_MCP_TOOL_NAMES)


# ============================================================ 3. Tool schema 正确


def test_tool_schema_declares_student_id() -> None:
    """考试工具 ``inputSchema`` 声明 ``student_id: string``、必填，并带上参数说明。

    参数说明不是装饰：MCP 客户端（含本项目的适配层）会把 ``inputSchema``
    当作唯一事实来源，缺说明时模型只能靠参数名猜含义。
    """

    tool = _tool_named(build_server(), TOOL_GET_EXAM_SCHEDULE)

    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema["properties"]["student_id"]["type"] == "string"
    assert "student_id" in schema["required"]
    assert schema["properties"]["student_id"]["description"]


def test_tool_description_explains_data_source_and_fields() -> None:
    """描述要说清「实时数据」与四个返回字段——它会被模型当作说明书读。"""

    tool = _tool_named(build_server(), TOOL_GET_EXAM_SCHEDULE)

    description = tool.description or ""
    assert "考试" in description
    assert "实时" in description
    for field_name in ("course", "date", "time", "room"):
        assert field_name in description


# ============================================================ 4. Tool 参数正确


def test_tool_receives_student_id_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """入参被原样交给业务服务（参数没被吞掉、没被改名）。"""

    seen: list[str | None] = []

    class RecordingClient:
        def fetch_exam_schedule(self, student_id: str | None = None) -> list[dict[str, str]]:
            seen.append(student_id)
            return list(EXAM_SCHEDULE_PAYLOAD)

    patch_campus_api_in(monkeypatch, server_module, RecordingClient())  # type: ignore[arg-type]

    _call_tool(build_server(), {"student_id": "student-777"})

    assert seen == ["student-777"]


def test_missing_required_argument_is_rejected_by_the_schema() -> None:
    """缺少必填参数由 SDK 的入参校验拦截（不需要工具自己判断）。"""

    with pytest.raises(ToolError):
        _call_tool(build_server(), {})


# ================================================ 5. Tool 调用可以进入 CampusApiClient


def test_tool_call_reaches_the_campus_api_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用链真的走通了 Server 工具 -> CampusApiClient（而不是另有数据来源）。"""

    requests: list[Any] = []
    client = patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(default_student_id="student-001", requests=requests),
    )

    result = _call_tool(build_server(), {"student_id": "student-001"})

    assert len(requests) == 1, "MCP 工具必须真的经由 CampusApiClient 发起了请求"
    assert dict(requests[0].url.params) == {"studentId": "student-001"}
    assert client is not None
    assert result.is_error is False


def test_tool_returns_contract_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回结构符合对外契约：四字段字符串，按业务服务给出的顺序。"""

    patch_campus_api_in(monkeypatch, server_module, make_campus_client())

    result = _call_tool(build_server(), {"student_id": "student-001"})

    payload = result.structured_content
    assert payload == {"result": EXAM_SCHEDULE_PAYLOAD}


def test_tool_output_follows_the_service_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一份服务响应，MCP 工具输出随之改变。"""

    patch_campus_api_in(
        monkeypatch,
        server_module,
        make_campus_client(
            payload=[{"course": "量子力学", "date": "2027-01-05", "time": "08:30", "room": "D101"}]
        ),
    )

    result = _call_tool(build_server(), {"student_id": "student-001"})

    assert result.structured_content == {
        "result": [{"course": "量子力学", "date": "2027-01-05", "time": "08:30", "room": "D101"}]
    }


# ============================================================ 业务失败的可读性


def test_business_failure_is_raised_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """可预期的业务失败必须走 ``ToolError``。

    这是**协议层面的硬要求**：按 MCP SDK 的约定，只有 ``ToolError`` 会把原始
    消息交给客户端；其它异常一律被替换成 ``Error executing tool <name>``，
    业务原因就到不了 Agent。因此本用例守的是「用户能不能看懂失败原因」。
    """

    class FailingClient:
        def fetch_exam_schedule(self, student_id: str | None = None) -> list[dict[str, str]]:
            raise CampusServiceUnavailable("校园业务服务暂时不可用，请稍后再试。")

    patch_campus_api_in(monkeypatch, server_module, FailingClient())  # type: ignore[arg-type]

    with pytest.raises(ToolError) as excinfo:
        _call_tool(build_server(), {"student_id": "student-001"})

    assert "校园业务服务暂时不可用" in str(excinfo.value)


# ============================================================ 结构守卫（禁止项）


def test_server_never_reaches_the_database_or_http_layers() -> None:
    """MCP Server 不得连 MySQL、不得写 SQL、不得自己发 HTTP。

    数据只允许经 ``CampusApiClient`` 取得，HTTP 细节仍归基础设施层。
    """

    source = _source_of(server_module)
    imports = " ".join(
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    )

    for forbidden in (
        "mysql",
        "pymysql",
        "sqlalchemy",
        "sqlite3",
        "psycopg",
        "httpx",
        "requests",
        "app.infrastructure.campus_api.client",
    ):
        assert forbidden not in imports, f"MCP Server 不得直接依赖 {forbidden!r}"

    # 复用既有客户端，而不是自己拼 URL
    assert "get_campus_api_client()" in source
    assert ".fetch_exam_schedule(" in source
    assert ".fetch_course_schedule(" in source
    assert ".fetch_scores(" in source
    assert ".submit_leave_application(" in source
    # 不写 SQL
    for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert keyword not in source
    # 不自己拼请求路径：/api/* 只属于基础设施层（CampusApiClient）
    assert "/api/exams" not in source
    assert "/api/courses" not in source
    assert "/api/scores" not in source
    assert "/api/leaves" not in source
