"""校园业务服务 HTTP 客户端测试（完全离线）。

替身策略：用 ``httpx.MockTransport`` 替换「数据包从哪里来」，但
``CampusApiClient`` 的代码路径被真实执行——URL 拼接、query 参数、状态码分支、
超时/连接异常、JSON 解析。因此这些用例既能确定性运行，又真的有覆盖率。

本文件回答两类问题：
1. 正常路径：请求长什么样、响应怎么被解析出来；
2. 失败路径：五种失败（不可达 / 超时 / 4xx / 5xx / 格式异常）分别被翻译成
   哪个异常、消息是否**可被模型直接转述给用户**（不含 URL、不含堆栈）。

配置读取与校验见 ``tests/test_config.py``。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.infrastructure.campus_api import (
    EXAM_SCHEDULE_PATH,
    CampusApiClient,
    CampusApiError,
    CampusServiceDataError,
    CampusServiceRequestError,
    CampusServiceServerError,
    CampusServiceUnavailable,
)
from app.infrastructure.campus_api.client import _direct_mounts
from tests._fakes import (
    CAMPUS_TEST_BASE_URL,
    EXAM_SCHEDULE_PAYLOAD,
    make_campus_client,
)

STUDENT_ID = "student-001"


# ============================================================ 正常请求与解析


def test_successful_request_hits_the_expected_endpoint() -> None:
    """请求方法与路径、query 参数都符合 Spring Boot 侧契约。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(requests=requests)

    rows = client.fetch_exam_schedule()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == EXAM_SCHEDULE_PATH  # /api/exams
    assert dict(request.url.params) == {"studentId": STUDENT_ID}
    assert str(request.url).startswith(CAMPUS_TEST_BASE_URL)
    assert rows == EXAM_SCHEDULE_PAYLOAD


def test_configured_student_id_is_the_default_query_value() -> None:
    """未显式传学号时，用构造时配置的当前学生。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(default_student_id="student-777", requests=requests)

    client.fetch_exam_schedule()

    assert dict(requests[0].url.params) == {"studentId": "student-777"}


def test_explicit_student_id_overrides_the_default() -> None:
    """显式传入的学号优先于配置默认值。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(requests=requests)

    client.fetch_exam_schedule(student_id="student-042")

    assert dict(requests[0].url.params) == {"studentId": "student-042"}


def test_empty_result_is_not_an_error() -> None:
    """学生没有考试时返回空列表：这是正常结果，不是异常。"""

    assert make_campus_client(payload=[]).fetch_exam_schedule() == []


def test_base_url_trailing_slash_does_not_double_up() -> None:
    """基址尾部的斜杠不会拼出 ``//api/exams``。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(base_url="http://campus.test/", requests=requests)

    client.fetch_exam_schedule()

    assert requests[0].url.path == EXAM_SCHEDULE_PATH


# ============================================================ 失败路径分类


def test_timeout_becomes_unavailable() -> None:
    """请求超时 → 服务不可用（可重试语义）。"""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("读取超时", request=request)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        make_campus_client(timeout_handler).fetch_exam_schedule()

    assert "超时" in str(excinfo.value)


def test_connection_failure_becomes_unavailable() -> None:
    """连接被拒（服务没启动）→ 服务不可用。"""

    def refused_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        make_campus_client(refused_handler).fetch_exam_schedule()

    assert "暂时不可用" in str(excinfo.value)


@pytest.mark.parametrize("status_code", [400, 401, 404, 422])
def test_http_4xx_becomes_request_error(status_code: int) -> None:
    """4xx → 请求被拒绝（参数/权限语义，模型应改为修正查询条件）。"""

    with pytest.raises(CampusServiceRequestError) as excinfo:
        make_campus_client(status_code=status_code).fetch_exam_schedule()

    assert str(status_code) in str(excinfo.value)


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_http_5xx_becomes_server_error(status_code: int) -> None:
    """5xx → 服务端错误（模型应表述为「稍后再试」）。"""

    with pytest.raises(CampusServiceServerError) as excinfo:
        make_campus_client(status_code=status_code).fetch_exam_schedule()

    assert str(status_code) in str(excinfo.value)


def test_invalid_json_becomes_data_error() -> None:
    """返回体不是 JSON（例如被反向代理换成 HTML 错误页）→ 格式异常。"""

    with pytest.raises(CampusServiceDataError) as excinfo:
        make_campus_client(raw_body="<html>502 Bad Gateway</html>").fetch_exam_schedule()

    assert "格式异常" in str(excinfo.value)


def test_non_array_payload_becomes_data_error() -> None:
    """JSON 合法但不是数组 → 格式异常。"""

    with pytest.raises(CampusServiceDataError):
        make_campus_client(payload={"course": "数据结构"}).fetch_exam_schedule()


def test_non_object_item_becomes_data_error() -> None:
    """数组元素不是对象 → 格式异常，并指出是第几条。"""

    with pytest.raises(CampusServiceDataError) as excinfo:
        make_campus_client(payload=["数据结构"]).fetch_exam_schedule()

    assert "第 1 条" in str(excinfo.value)


# ============================================================ 错误消息可读性

#: 这些子串一旦出现在异常消息里，就等于把技术细节泄漏给模型与用户
_LEAKY_TOKENS = (
    "Traceback",
    "http://",
    "https://",
    "httpx",
    "ConnectError",
    "ReadTimeout",
    "MockTransport",
    "/api/",
    "None",
)


def _refused_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Connection refused", request=request)


def _timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timed out", request=request)


#: 全部失败类型 -> 客户端构造器。每个用例自己构造，避免在参数化期间共享客户端实例。
_FAILURE_BUILDERS: dict[str, Callable[[], CampusApiClient]] = {
    "unavailable": lambda: make_campus_client(_refused_handler),
    "timeout": lambda: make_campus_client(_timeout_handler),
    "http-4xx": lambda: make_campus_client(status_code=404),
    "http-5xx": lambda: make_campus_client(status_code=500),
    "invalid-json": lambda: make_campus_client(raw_body="<html>502 Bad Gateway</html>"),
    "non-array": lambda: make_campus_client(payload={"unexpected": 1}),
    "non-object-item": lambda: make_campus_client(payload=["数据结构"]),
}


@pytest.mark.parametrize("label", sorted(_FAILURE_BUILDERS))
def test_failure_messages_are_user_facing(label: str) -> None:
    """失败消息必须是「可转述给用户的中文」，不含 URL 与堆栈。

    这条约束很重要：工具异常会被 ``handle_tool_error`` **原样**写进
    ``ToolMessage`` 交给模型，所以异常文案就是产品文案。
    """

    client = _FAILURE_BUILDERS[label]()

    with pytest.raises(CampusApiError) as excinfo:
        client.fetch_exam_schedule()

    message = str(excinfo.value)
    assert message.endswith("。"), f"{label}: {message}"
    for token in _LEAKY_TOKENS:
        assert token not in message, f"{label} 的异常消息泄漏了技术细节 {token!r}: {message}"
    assert "校园业务" in message


def test_exception_family_shares_one_base_type() -> None:
    """四类失败同源：调用方可以只 catch ``CampusApiError`` 兜住全部。"""

    for exc_type in (
        CampusServiceUnavailable,
        CampusServiceRequestError,
        CampusServiceServerError,
        CampusServiceDataError,
    ):
        assert issubclass(exc_type, CampusApiError)
        assert issubclass(exc_type, RuntimeError)


# ============================================================ 生命周期


def test_close_releases_self_created_client() -> None:
    """自建客户端被 close 后不可再用（避免连接池泄漏后静默复用）。"""

    client = CampusApiClient(CAMPUS_TEST_BASE_URL)

    client.close()

    with pytest.raises(RuntimeError):
        client.fetch_exam_schedule()


def test_close_does_not_close_injected_client() -> None:
    """注入的客户端归调用方所有：本类只关自己建的那个。"""

    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    )
    client = CampusApiClient(CAMPUS_TEST_BASE_URL, client=http_client)

    client.close()

    assert not http_client.is_closed
    assert client.fetch_exam_schedule() == []
    http_client.close()


def test_client_is_reusable_across_calls() -> None:
    """同一个客户端可重复使用（连接池复用，不每次新建）。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(requests=requests)

    client.fetch_exam_schedule()
    client.fetch_exam_schedule()

    assert len(requests) == 2


# ============================================================ 回环地址直连


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:8080", 2),
        ("http://127.0.0.1:8081", 2),
        ("http://[::1]:8080", 2),
        ("http://campus.test:8080", 0),
        ("https://campus.example.edu", 0),
        ("", 0),
    ],
)
def test_direct_mounts_only_apply_to_loopback(base_url: str, expected: int) -> None:
    """只有本机地址直连；远端地址保留环境代理设置（不破坏内网代理）。"""

    assert len(_direct_mounts(base_url)) == expected


def test_loopback_mount_keys_are_valid_url_prefixes() -> None:
    """挂载键必须是合法 URL 前缀，IPv6 需带方括号。"""

    assert set(_direct_mounts("http://[::1]:8080")) == {"http://[::1]", "https://[::1]"}


def _unused_loopback_port() -> int:
    """取一个当前无人监听的回环端口（用作「打不通的代理」）。"""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_loopback_traffic_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机地址直连：不把「服务到底通不通」交给环境代理回答。

    复现的是联调时真实踩到的问题——开发机设置了 ``HTTP_PROXY``，本机 8081 上的
    Spring Boot 停掉后，请求被代理接走并返回 ``502 Bad Gateway``，
    于是「服务不可达」被误判成「服务端错误」，异常分类与建议都跟着失真。

    本用例用一个真实监听在回环地址上的最小 HTTP 服务充当校园业务服务，
    同时把环境代理指向一个无人监听的端口，并做**双向断言**：
    直连的客户端必须成功，而仍读环境代理的客户端必须失败——
    后者证明前者的成功确实来自「直连」，而不是环境里本来就没有代理。
    """

    payload = json.dumps(EXAM_SCHEDULE_PAYLOAD, ensure_ascii=False).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定命名
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            """静默访问日志，避免污染 pytest 输出。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    dead_proxy = f"http://127.0.0.1:{_unused_loopback_port()}"
    monkeypatch.setenv("HTTP_PROXY", dead_proxy)
    monkeypatch.setenv("HTTPS_PROXY", dead_proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    try:
        # 1) 自建客户端：回环直连，不受代理影响
        assert CampusApiClient(base_url, timeout=3.0).fetch_exam_schedule() == EXAM_SCHEDULE_PAYLOAD

        # 2) 反证：未做直连处理的客户端会被代理拖去死端口，请求失败
        with pytest.raises(CampusServiceUnavailable):
            CampusApiClient(
                base_url, timeout=3.0, client=httpx.Client(timeout=3.0)
            ).fetch_exam_schedule()
    finally:
        server.shutdown()
        server.server_close()
