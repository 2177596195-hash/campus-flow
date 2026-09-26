"""MCP Client 用例：**真实** stdio 往返（起子进程、走 MCP 协议）。

为什么必须真起子进程
--------------------

「Client 能连接 Server / 能发现工具 / 能读 schema / 能调用」这几件事只有在
**真实协议往返**下才算被证明：进程内调用绕过了 stdio 编解码、握手、能力协商、
``tools/list`` 这条请求/响应路径，恰好是最容易出问题的一段。

Spring Boot 用**本机回环 stub HTTP 服务**替代：

- 子进程拿到的 ``CAMPUS_SERVICE_BASE_URL`` 指向 ``127.0.0.1:<随机端口>``，
  由本文件的 ``ThreadingHTTPServer`` 提供与 MySQL 种子数据一致的 JSON；
- 因此测试**不需要 MySQL、不需要 Spring Boot、不访问外网**，只走回环；
- 同时它仍然真实覆盖了「MCP Server -> CampusApiClient -> HTTP -> JSON 解析」
  这一整段（这正是把 stub 放在 HTTP 层而不是 fake 掉 CampusApiClient 的价值）。

一个模块级夹具共享同一条会话，因此整个文件只拉起一次 MCP Server 子进程。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from app.config.settings import app_root
from app.mcp.client import (
    GENERIC_TOOL_FAILURE,
    MCPToolInvocationError,
    MCPToolNotFound,
    StdioMCPClient,
)
from tests._fakes import (
    COURSE_SCHEDULE_PAYLOAD,
    EXAM_SCHEDULE_PAYLOAD,
    SCORE_PAYLOAD,
    course_schedule_payload,
    score_payload,
)

EXAM_PATH = "/api/exams"
COURSE_PATH = "/api/courses"
SCORE_PATH = "/api/scores"

#: 按学号返回不同数据，用于证明「参数真的传到了 Server 并影响了结果」
STUB_RESPONSES: dict[str, list[dict[str, str]]] = {
    "student-001": EXAM_SCHEDULE_PAYLOAD,
    "student-002": [
        {"course": "大学英语", "date": "2026-09-29", "time": "09:00", "room": "C105"}
    ],
}

#: 有课表的学号。不在名单里的学号返回空数组（用于验证「查不到不是错误」）。
STUDENTS_WITH_COURSES: frozenset[str] = frozenset({"student-001"})

#: 有成绩的学号。不在名单里的学号返回空数组（同上）。
STUDENTS_WITH_SCORES: frozenset[str] = frozenset({"student-001"})

#: 触发「业务服务内部错误」的学号（stub 对它返回 503）
STATUS_ERROR_STUDENT_ID = "student-500"


class _StubCampusHandler(BaseHTTPRequestHandler):
    """替代 Spring Boot 的最小 GET 服务（实现 ``/api/exams``、``/api/courses``、``/api/scores``）。

    课程侧按 ``week`` 查询参数过滤、成绩侧按 ``semester`` 查询参数过滤，
    过滤规则与 Spring Boot 对应 Service 一致——否则「``week`` / ``semester``
    真的被透传下去了」就无法通过协议往返被观察到。
    """

    #: 依次收到的考试查询 studentId，供用例断言参数透传
    received_student_ids: list[str] = []
    #: 依次收到的课程查询 ``(studentId, week)``，``week`` 为原始字符串或 ``None``
    received_course_queries: list[tuple[str, str | None]] = []
    #: 依次收到的成绩查询**完整 query 字典**。刻意记录整个 query 而不是挑两个键：
    #: 「``course`` 被误当成 query 参数发给 Spring Boot」正是要靠它才看得见。
    received_score_queries: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == EXAM_PATH:
            self._serve_exam(params)
            return
        if parsed.path == COURSE_PATH:
            self._serve_course(params)
            return
        if parsed.path == SCORE_PATH:
            self._serve_score(params)
            return
        self._respond(404, {"status": 404})

    def _serve_exam(self, params: dict[str, list[str]]) -> None:
        student_id = params.get("studentId", [""])[0]
        type(self).received_student_ids.append(student_id)

        if student_id == STATUS_ERROR_STUDENT_ID:
            self._respond(503, {"error": "boom"})
            return

        self._respond(200, STUB_RESPONSES.get(student_id, []))

    def _serve_course(self, params: dict[str, list[str]]) -> None:
        student_id = params.get("studentId", [""])[0]
        raw_week = params.get("week", [None])[0]
        type(self).received_course_queries.append((student_id, raw_week))

        if student_id == STATUS_ERROR_STUDENT_ID:
            self._respond(503, {"error": "boom"})
            return
        if student_id not in STUDENTS_WITH_COURSES:
            self._respond(200, [])
            return

        week = int(raw_week) if raw_week else None
        self._respond(200, course_schedule_payload(week=week))

    def _serve_score(self, params: dict[str, list[str]]) -> None:
        type(self).received_score_queries.append(
            {key: values[0] for key, values in params.items()}
        )
        student_id = params.get("studentId", [""])[0]
        raw_semester = params.get("semester", [None])[0]

        if student_id == STATUS_ERROR_STUDENT_ID:
            self._respond(503, {"error": "boom"})
            return
        if student_id not in STUDENTS_WITH_SCORES:
            self._respond(200, [])
            return

        self._respond(200, score_payload(semester=raw_semester or None))

    def _respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """静音访问日志（默认会写 stderr，干扰 pytest 输出）。"""


@pytest.fixture(scope="module")
def stub_campus_service() -> Any:
    """本机回环 stub 业务服务（随机端口），替代 Spring Boot。"""

    _StubCampusHandler.received_student_ids = []
    _StubCampusHandler.received_course_queries = []
    _StubCampusHandler.received_score_queries = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubCampusHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="stub-campus", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def mcp_client(stub_campus_service: str) -> Any:
    """按**生产默认启动配方**拉起真实 MCP Server 的客户端。"""

    env = {**os.environ, "CAMPUS_SERVICE_BASE_URL": stub_campus_service}
    client = StdioMCPClient(
        sys.executable,
        ("-m", "app.mcp.server"),
        env=env,
        cwd=app_root(),
        startup_timeout=60.0,
        call_timeout=30.0,
    )
    client.start()
    try:
        yield client
    finally:
        client.close()


# ============================================================ 6. 能连接 stdio Server


def test_client_connects_to_the_stdio_server(mcp_client: StdioMCPClient) -> None:
    """握手完成、会话建立（``initialize`` 已返回）。"""

    assert mcp_client.started is True
    assert mcp_client.server_name == "campus-mcp-server"
    assert mcp_client.command_line[1:] == ("-m", "app.mcp.server")


def test_client_start_is_idempotent(mcp_client: StdioMCPClient) -> None:
    """重复 ``start()`` 复用既有会话，不会拉起第二个 Server 进程。"""

    mcp_client.start()
    mcp_client.start()

    assert mcp_client.started is True


# ================================================ 7. 能发现 get_exam_schedule


def test_client_discovers_the_exam_tool(mcp_client: StdioMCPClient) -> None:
    """``tools/list`` 的结果里能看到 ``get_exam_schedule``。"""

    names = [tool.name for tool in mcp_client.list_tools()]

    assert "get_exam_schedule" in names


def test_client_discovers_every_registered_tool(mcp_client: StdioMCPClient) -> None:
    """四个工具（三个查询 + 一个写操作）由**同一次** ``tools/list`` 返回。

    这条同时守住「不为某项能力另起第二个 Server」：若某项能力（例如请假提交）
    被拆到另一个进程，当前这条会话里就看不到它。
    """

    names = [tool.name for tool in mcp_client.list_tools()]

    assert names == [
        "get_exam_schedule",
        "get_course_schedule",
        "get_score",
        "submit_leave_application",
    ]


def test_client_reports_unknown_tool_clearly(mcp_client: StdioMCPClient) -> None:
    """发现结果里没有的工具要报「没有这个工具」，而不是调用后才失败。"""

    with pytest.raises(MCPToolNotFound) as excinfo:
        mcp_client.find_tool("get_leave_request")

    assert "get_leave_request" in str(excinfo.value)
    assert "get_exam_schedule" in str(excinfo.value)  # 提示可用项


# ============================================================ 8. 能读取 schema


def test_client_reads_tool_schema(mcp_client: StdioMCPClient) -> None:
    """能读到 ``description`` 与 ``inputSchema``（适配层就靠它生成 args_schema）。"""

    tool = mcp_client.find_tool("get_exam_schedule")

    assert tool.description and "考试" in tool.description
    assert tool.input_schema["properties"]["student_id"]["type"] == "string"
    assert "student_id" in tool.input_schema["required"]


# ==================================================== 9 / 10. 能调用并拿到结构


def test_client_calls_the_tool_over_the_wire(mcp_client: StdioMCPClient) -> None:
    """真实调用：跨进程 -> MCP Server -> CampusApiClient -> stub HTTP。"""

    payload = mcp_client.call_tool("get_exam_schedule", {"student_id": "student-001"})

    assert payload == EXAM_SCHEDULE_PAYLOAD


def test_tool_arguments_reach_the_campus_service(mcp_client: StdioMCPClient) -> None:
    """参数透传到底：stub 服务收到了我们传的 studentId。"""

    payload = mcp_client.call_tool("get_exam_schedule", {"student_id": "student-002"})

    assert payload == STUB_RESPONSES["student-002"]
    assert _StubCampusHandler.received_student_ids[-1] == "student-002"


def test_empty_result_is_not_an_error(mcp_client: StdioMCPClient) -> None:
    """查不到考试返回空列表，而不是失败。"""

    assert mcp_client.call_tool("get_exam_schedule", {"student_id": "student-999"}) == []


def test_client_returns_contract_shape(mcp_client: StdioMCPClient) -> None:
    """返回结构是「四字段字符串的列表」——与直连工具的契约完全一致。"""

    payload = mcp_client.call_tool("get_exam_schedule", {"student_id": "student-001"})

    assert isinstance(payload, list)
    for row in payload:
        assert set(row) == {"course", "date", "time", "room"}
        assert all(isinstance(value, str) for value in row.values())


# ======================================= 课程工具（真实协议往返）

#: 课程工具的六字段契约（与直连工具完全一致）
COURSE_CONTRACT_FIELDS = {"course", "teacher", "weekday", "time", "room", "weeks"}


def test_course_tool_schema_is_discoverable(mcp_client: StdioMCPClient) -> None:
    """能读到课程工具的 ``description`` 与 ``inputSchema``（适配层靠它建 args_schema）。"""

    tool = mcp_client.find_tool("get_course_schedule")

    assert tool.description and "课" in tool.description
    schema = tool.input_schema
    assert schema["properties"]["student_id"]["type"] == "string"
    assert "student_id" in schema["required"]
    # week 可选：出现在 properties 里，但不在 required 里
    assert "week" in schema["properties"]
    assert "week" not in schema.get("required", [])


def test_client_calls_the_course_tool_over_the_wire(mcp_client: StdioMCPClient) -> None:
    """真实调用：跨进程 -> MCP Server -> CampusApiClient -> stub HTTP。"""

    payload = mcp_client.call_tool("get_course_schedule", {"student_id": "student-001"})

    assert payload == COURSE_SCHEDULE_PAYLOAD
    assert isinstance(payload, list)
    for row in payload:
        assert set(row) == COURSE_CONTRACT_FIELDS


def test_course_week_argument_reaches_the_campus_service(mcp_client: StdioMCPClient) -> None:
    """``week`` 透传到底：stub 服务收到了 ``week=3``，且结果随之变化。"""

    payload = mcp_client.call_tool(
        "get_course_schedule", {"student_id": "student-001", "week": 3}
    )

    assert payload == course_schedule_payload(week=3)
    assert _StubCampusHandler.received_course_queries[-1] == ("student-001", "3")
    # 数据库原理第 5 周才开课：第 3 周查不到 → 过滤真的在服务端生效了
    assert "数据库原理" not in {row["course"] for row in payload}


def test_course_week_is_optional_over_the_wire(mcp_client: StdioMCPClient) -> None:
    """不传 ``week`` 时请求里就不带该参数（由服务端返回整学期课表）。"""

    mcp_client.call_tool("get_course_schedule", {"student_id": "student-001"})

    assert _StubCampusHandler.received_course_queries[-1] == ("student-001", None)


def test_course_empty_result_is_not_an_error(mcp_client: StdioMCPClient) -> None:
    """该学生没课返回空列表，而不是失败。"""

    assert mcp_client.call_tool("get_course_schedule", {"student_id": "student-999"}) == []


def test_course_business_error_is_reported_readably(mcp_client: StdioMCPClient) -> None:
    """课程链路的 5xx 同样以可读中文上抛（与考试链路一致）。"""

    with pytest.raises(MCPToolInvocationError) as excinfo:
        mcp_client.call_tool(
            "get_course_schedule", {"student_id": STATUS_ERROR_STUDENT_ID}
        )

    message = str(excinfo.value)
    assert "校园业务服务内部错误" in message
    assert "Error executing tool" not in message
    assert "Traceback" not in message
    assert message != GENERIC_TOOL_FAILURE


def test_course_invalid_week_is_rejected_readably(mcp_client: StdioMCPClient) -> None:
    """``week`` 越界由服务端校验拦截，原因可读（不是一句英文协议错误）。"""

    with pytest.raises(MCPToolInvocationError) as excinfo:
        mcp_client.call_tool(
            "get_course_schedule", {"student_id": "student-001", "week": 99}
        )

    message = str(excinfo.value)
    assert "week" in message
    assert "Error executing tool" not in message


# ======================================= 成绩工具（真实协议往返）

#: 成绩工具的四字段契约（与直连工具完全一致；``score`` / ``credit`` 是数值）
SCORE_CONTRACT_FIELDS = {"semester", "course", "score", "credit"}


def test_score_tool_schema_is_discoverable(mcp_client: StdioMCPClient) -> None:
    """能读到成绩工具的 ``description`` 与 ``inputSchema``（适配层靠它建 args_schema）。"""

    tool = mcp_client.find_tool("get_score")

    assert tool.description and "成绩" in tool.description
    schema = tool.input_schema
    assert schema["properties"]["student_id"]["type"] == "string"
    assert "student_id" in schema["required"]
    # semester / course 可选：出现在 properties 里，但不在 required 里
    for optional in ("semester", "course"):
        assert optional in schema["properties"]
        assert optional not in schema.get("required", [])


def test_client_calls_the_score_tool_over_the_wire(mcp_client: StdioMCPClient) -> None:
    """真实调用：跨进程 -> MCP Server -> CampusApiClient -> stub HTTP。"""

    payload = mcp_client.call_tool("get_score", {"student_id": "student-001"})

    assert payload == SCORE_PAYLOAD
    assert isinstance(payload, list)
    for row in payload:
        assert set(row) == SCORE_CONTRACT_FIELDS
        # 分数与学分必须是数字（模型要比较大小、做加权）
        assert isinstance(row["score"], (int, float))
        assert isinstance(row["credit"], (int, float))


def test_score_semester_argument_reaches_the_campus_service(
    mcp_client: StdioMCPClient,
) -> None:
    """``semester`` 透传到底：stub 服务收到该 query 参数，且结果随学期变化。"""

    payload = mcp_client.call_tool(
        "get_score", {"student_id": "student-001", "semester": "2024-2025-2"}
    )

    assert _StubCampusHandler.received_score_queries[-1] == {
        "studentId": "student-001",
        "semester": "2024-2025-2",
    }
    assert payload == score_payload(semester="2024-2025-2")
    # 服务端过滤真的生效：上学期只有两门，且不含本学期课程
    assert {row["course"] for row in payload} == {"高等数学", "大学英语"}


def test_score_omits_both_optional_arguments_when_absent(
    mcp_client: StdioMCPClient,
) -> None:
    """两个可选参数都不传时，请求里**一个都不出现**（更不会冒出 ``course``）。"""

    mcp_client.call_tool("get_score", {"student_id": "student-001"})

    assert _StubCampusHandler.received_score_queries[-1] == {"studentId": "student-001"}


def test_score_course_is_not_sent_to_the_service(mcp_client: StdioMCPClient) -> None:
    """需求 8：``course`` **不得**作为 query 参数发给 Spring Boot（接口不支持它）。"""

    payload = mcp_client.call_tool(
        "get_score", {"student_id": "student-001", "course": "计算机网络"}
    )

    query = _StubCampusHandler.received_score_queries[-1]
    assert "course" not in query
    assert query == {"studentId": "student-001"}
    # 课程筛选在 Python 侧完成：结果只剩这一门
    assert [row["course"] for row in payload] == ["计算机网络"]


def test_score_empty_result_is_not_an_error(mcp_client: StdioMCPClient) -> None:
    """该学生没成绩返回空列表，而不是失败。"""

    assert mcp_client.call_tool("get_score", {"student_id": "student-999"}) == []


def test_score_business_error_is_reported_readably(mcp_client: StdioMCPClient) -> None:
    """成绩链路的 5xx 同样以可读中文上抛（与考试 / 课程链路一致）。"""

    with pytest.raises(MCPToolInvocationError) as excinfo:
        mcp_client.call_tool("get_score", {"student_id": STATUS_ERROR_STUDENT_ID})

    message = str(excinfo.value)
    assert "校园业务服务内部错误" in message
    assert "Error executing tool" not in message
    assert "Traceback" not in message
    assert message != GENERIC_TOOL_FAILURE


def test_score_invalid_argument_type_is_rejected_readably(
    mcp_client: StdioMCPClient,
) -> None:
    """``semester`` 类型错误由入参校验拦截，且原因里点得出是哪个参数。"""

    with pytest.raises(MCPToolInvocationError) as excinfo:
        mcp_client.call_tool("get_score", {"student_id": "student-001", "semester": 2025})

    message = str(excinfo.value)
    assert "semester" in message
    assert "Error executing tool" not in message


# ============================================================ 失败可读性


def test_business_error_is_reported_readably(mcp_client: StdioMCPClient) -> None:
    """业务服务 5xx 时，Client 拿到的是可读中文，而不是协议原文。

    SDK 会把工具错误统一包成 ``Error executing tool <name>[: <原始消息>]``；
    适配层负责剥掉前缀，否则模型/用户看到的是一句无意义的英文。
    """

    with pytest.raises(MCPToolInvocationError) as excinfo:
        mcp_client.call_tool("get_exam_schedule", {"student_id": STATUS_ERROR_STUDENT_ID})

    message = str(excinfo.value)
    assert "校园业务服务内部错误" in message
    assert "Error executing tool" not in message
    assert message != GENERIC_TOOL_FAILURE
    assert "Traceback" not in message


def test_close_is_safe_before_start() -> None:
    """未建连就 ``close()`` 不应抛错（生命周期收尾要能无条件调用）。

    刻意用一个独立客户端：``close()`` 会终止会话，不能对模块级共享夹具做这件事。
    """

    client = StdioMCPClient(
        sys.executable, ("-m", "app.mcp.server"), cwd=app_root()
    )

    client.close()
    client.close()

    assert client.started is False
