"""校园业务服务「课程安排」客户端测试（完全离线）。

与 ``test_campus_api_client.py``（考试接口）同一套替身策略：用
``httpx.MockTransport`` 替换「数据包从哪里来」，而 ``CampusApiClient`` 的代码
路径被真实执行——URL 拼接、query 参数、状态码分支、超时/连接异常、JSON 解析。

本文件回答四类问题：

1. 请求长什么样：路径、方法、query 参数（``studentId`` 必带、``week`` 可选）；
2. ``week`` 是怎么过去的：作为 query 参数原样透传，**不在客户端做过滤**；
3. 响应怎么被解析出来（含空结果）；
4. 五种失败分别被翻译成哪个异常、消息是否可被模型直接转述给用户。
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
    COURSE_SCHEDULE_PATH,
    CampusApiClient,
    CampusApiError,
    CampusServiceDataError,
    CampusServiceRequestError,
    CampusServiceServerError,
    CampusServiceUnavailable,
)
from tests._fakes import (
    CAMPUS_TEST_BASE_URL,
    COURSE_SCHEDULE_PAYLOAD,
    DEFAULT_CAMPUS_PAYLOADS,
    EXAM_SCHEDULE_PAYLOAD,
    course_schedule_payload,
    make_campus_client,
)

STUDENT_ID = "student-001"


# ============================================================ 请求形态


def test_successful_request_hits_the_courses_endpoint() -> None:
    """请求方法与路径、query 参数都符合 Spring Boot 侧契约。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests)

    rows = client.fetch_course_schedule()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == COURSE_SCHEDULE_PATH  # /api/courses
    assert dict(request.url.params) == {"studentId": STUDENT_ID}
    assert str(request.url).startswith(CAMPUS_TEST_BASE_URL)
    assert rows == COURSE_SCHEDULE_PAYLOAD


def test_courses_and_exams_are_separate_endpoints() -> None:
    """默认替身的按路径路由：课程请求不会被考试响应串台（反之亦然）。"""

    client = make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS)

    assert client.fetch_course_schedule() == COURSE_SCHEDULE_PAYLOAD
    assert client.fetch_exam_schedule() == EXAM_SCHEDULE_PAYLOAD
    # 两份数据形态确实不同，上面的断言才有意义
    assert set(COURSE_SCHEDULE_PAYLOAD[0]) != set(EXAM_SCHEDULE_PAYLOAD[0])


def test_base_url_trailing_slash_does_not_double_up() -> None:
    """基址尾部的斜杠不会拼出 ``//api/courses``。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(
        payload=COURSE_SCHEDULE_PAYLOAD, base_url="http://campus.test/", requests=requests
    )

    client.fetch_course_schedule()

    assert requests[0].url.path == COURSE_SCHEDULE_PATH


# ============================================================ 参数透传


def test_configured_student_id_is_the_default_query_value() -> None:
    """未显式传学号时，用构造时配置的当前学生。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(
        payload=COURSE_SCHEDULE_PAYLOAD, default_student_id="student-777", requests=requests
    )

    client.fetch_course_schedule()

    assert dict(requests[0].url.params) == {"studentId": "student-777"}


def test_explicit_student_id_overrides_the_default() -> None:
    """显式传入的学号优先于配置默认值。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests)

    client.fetch_course_schedule(student_id="student-042")

    assert dict(requests[0].url.params) == {"studentId": "student-042"}


def test_week_is_sent_as_a_query_parameter() -> None:
    """``week`` 以 query 参数形式发出，且与 ``studentId`` 同时存在。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests)

    client.fetch_course_schedule(week=3)

    assert dict(requests[0].url.params) == {"studentId": STUDENT_ID, "week": "3"}


def test_omitting_week_keeps_the_query_minimal() -> None:
    """不传 ``week`` 时不带该参数（服务端据此返回整学期课表）。

    显式传 ``week=None`` 与省略参数必须等价：客户端不该发出 ``week=``
    这样的空值，否则服务端要额外处理「空字符串 vs 缺参」两种形态。
    """

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests)

    client.fetch_course_schedule()
    client.fetch_course_schedule(week=None)

    assert [dict(request.url.params) for request in requests] == [
        {"studentId": STUDENT_ID},
        {"studentId": STUDENT_ID},
    ]


@pytest.mark.parametrize("week", [1, 3, 18, 30])
def test_week_value_is_passed_through_verbatim(week: int) -> None:
    """周次原样透传：客户端不夹带偏移、不把 1 当成「未指定」。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests)

    client.fetch_course_schedule(week=week)

    assert requests[0].url.params["week"] == str(week)


def test_week_filtering_happens_on_the_server_not_in_the_client() -> None:
    """过滤在服务端：客户端只透传条件，返回什么由服务响应决定。

    这条边界值得显式守住——如果哪天有人把「按周过滤」搬到客户端，
    这里会出现「请求没带 week、结果却被过滤过」的矛盾，用例立刻失败。
    """

    requests: list[httpx.Request] = []
    client = make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests)

    rows = client.fetch_course_schedule(week=3)

    # 1) 条件确实发出去了
    assert dict(requests[0].url.params) == {"studentId": STUDENT_ID, "week": "3"}
    # 2) 返回内容与「服务端按同一条件过滤」的结果一致
    assert rows == course_schedule_payload(week=3)
    # 3) 第 5 周才开课的课程不在第 3 周（过滤真的发生了，而不是原样全返）
    assert "数据库原理" not in {row["course"] for row in rows}
    assert len(rows) < len(COURSE_SCHEDULE_PAYLOAD)


def test_empty_result_is_not_an_error() -> None:
    """该学生（或该周）没有课时返回空列表：这是正常结果，不是异常。"""

    assert make_campus_client(payload=[]).fetch_course_schedule() == []
    assert make_campus_client(payload=[]).fetch_course_schedule(week=3) == []


# ============================================================ 失败路径分类


def test_timeout_becomes_unavailable() -> None:
    """请求超时 -> 服务不可用（可重试语义）。"""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("读取超时", request=request)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        make_campus_client(timeout_handler).fetch_course_schedule()

    assert "超时" in str(excinfo.value)


def test_connection_failure_becomes_unavailable() -> None:
    """连接被拒（服务没启动）-> 服务不可用。"""

    def refused_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        make_campus_client(refused_handler).fetch_course_schedule(week=3)

    assert "暂时不可用" in str(excinfo.value)


@pytest.mark.parametrize("status_code", [400, 401, 404, 422])
def test_http_4xx_becomes_request_error(status_code: int) -> None:
    """4xx -> 请求被拒绝（参数/权限语义）。"""

    with pytest.raises(CampusServiceRequestError) as excinfo:
        make_campus_client(status_code=status_code).fetch_course_schedule()

    assert str(status_code) in str(excinfo.value)


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_http_5xx_becomes_server_error(status_code: int) -> None:
    """5xx -> 服务端错误（模型应表述为「稍后再试」）。"""

    with pytest.raises(CampusServiceServerError) as excinfo:
        make_campus_client(status_code=status_code).fetch_course_schedule()

    assert str(status_code) in str(excinfo.value)


def test_invalid_json_becomes_data_error() -> None:
    """返回体不是 JSON（例如被反向代理换成 HTML 错误页）-> 格式异常。"""

    with pytest.raises(CampusServiceDataError) as excinfo:
        make_campus_client(raw_body="<html>502 Bad Gateway</html>").fetch_course_schedule()

    assert "格式异常" in str(excinfo.value)


def test_non_array_payload_becomes_data_error() -> None:
    """JSON 合法但不是数组 -> 格式异常。"""

    with pytest.raises(CampusServiceDataError):
        make_campus_client(payload={"course": "数据结构"}).fetch_course_schedule()


def test_non_object_item_becomes_data_error() -> None:
    """数组元素不是对象 -> 格式异常，并指出是第几条。"""

    with pytest.raises(CampusServiceDataError) as excinfo:
        make_campus_client(payload=["数据结构"]).fetch_course_schedule(week=3)

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


#: 全部失败类型 -> 客户端构造器。每个用例自己构造，避免共享客户端实例。
_COURSE_FAILURE_BUILDERS: dict[str, Callable[[], CampusApiClient]] = {
    "unavailable": lambda: make_campus_client(_refused_handler),
    "timeout": lambda: make_campus_client(_timeout_handler),
    "http-4xx": lambda: make_campus_client(status_code=400),
    "http-5xx": lambda: make_campus_client(status_code=500),
    "invalid-json": lambda: make_campus_client(raw_body="<html>502 Bad Gateway</html>"),
    "non-array": lambda: make_campus_client(payload={"unexpected": 1}),
    "non-object-item": lambda: make_campus_client(payload=["数据结构"]),
}


@pytest.mark.parametrize("label", sorted(_COURSE_FAILURE_BUILDERS))
def test_failure_messages_are_user_facing(label: str) -> None:
    """失败消息必须是「可转述给用户的中文」，不含 URL 与堆栈。"""

    client = _COURSE_FAILURE_BUILDERS[label]()

    with pytest.raises(CampusApiError) as excinfo:
        client.fetch_course_schedule(week=3)

    message = str(excinfo.value)
    assert message.endswith("。"), f"{label}: {message}"
    for token in _LEAKY_TOKENS:
        assert token not in message, f"{label} 的异常消息泄漏了技术细节 {token!r}: {message}"
    assert "校园业务" in message


def test_courses_and_exams_share_the_same_exception_family() -> None:
    """接口不同、异常体系相同：调用方无需为课程单独写一套失败分支。"""

    with pytest.raises(CampusServiceUnavailable):
        make_campus_client(_refused_handler).fetch_course_schedule()

    with pytest.raises(CampusServiceUnavailable):
        make_campus_client(_refused_handler).fetch_exam_schedule()


# ============================================================ 回环直连（复用既有机制）


def _unused_loopback_port() -> int:
    """取一个当前无人监听的回环端口（用作「打不通的代理」）。"""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_loopback_course_traffic_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """课程接口与考试接口共用同一个客户端，回环直连规则同样生效。

    这里用一个真实的回环 HTTP 服务充当校园业务服务（返回课表），
    同时把环境代理指向一个无人监听的端口，做双向断言。
    """

    payload = json.dumps(COURSE_SCHEDULE_PAYLOAD, ensure_ascii=False).encode("utf-8")

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
        assert CampusApiClient(base_url, timeout=3.0).fetch_course_schedule() == COURSE_SCHEDULE_PAYLOAD

        # 反证：未做直连处理的客户端会被代理拖去死端口
        with pytest.raises(CampusServiceUnavailable):
            CampusApiClient(
                base_url, timeout=3.0, client=httpx.Client(timeout=3.0)
            ).fetch_course_schedule()
    finally:
        server.shutdown()
        server.server_close()
