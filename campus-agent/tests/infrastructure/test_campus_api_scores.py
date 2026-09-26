"""校园业务服务「成绩查询」客户端测试（完全离线）。

与 ``test_campus_api_courses.py``（课程接口）同一套替身策略：用
``httpx.MockTransport`` 替换「数据包从哪里来」，而 ``CampusApiClient`` 的代码
路径被真实执行——URL 拼接、query 参数、状态码分支、超时/连接异常、JSON 解析。

本文件回答四类问题：

1. 请求长什么样：路径、方法、query 参数（``studentId`` 必带、``semester`` 可选）；
2. ``semester`` 是怎么过去的：作为 query 参数原样透传，**不在客户端做过滤**；
3. 响应怎么被解析出来（含空结果）；
4. 五种失败分别被翻译成哪个异常、消息是否可被模型直接转述给用户。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.infrastructure.campus_api import (
    SCORE_PATH,
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
    SCORE_PAYLOAD,
    make_campus_client,
    score_payload,
)

STUDENT_ID = "student-001"
CURRENT_SEMESTER = "2025-2026-1"


# ============================================================ 请求形态


def test_successful_request_hits_the_scores_endpoint() -> None:
    """请求方法与路径、query 参数都符合 Spring Boot 侧契约。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=SCORE_PAYLOAD, requests=requests)

    rows = client.fetch_scores()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == SCORE_PATH  # /api/scores
    assert dict(request.url.params) == {"studentId": STUDENT_ID}
    assert str(request.url).startswith(CAMPUS_TEST_BASE_URL)
    assert rows == SCORE_PAYLOAD


def test_scores_and_other_endpoints_are_separate() -> None:
    """默认替身的按路径路由：成绩请求不会被考试/课程响应串台（反之亦然）。"""

    client = make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS)

    assert client.fetch_scores() == SCORE_PAYLOAD
    assert client.fetch_course_schedule() == COURSE_SCHEDULE_PAYLOAD
    assert client.fetch_exam_schedule() == EXAM_SCHEDULE_PAYLOAD
    # 三份数据形态确实不同，上面的断言才有意义
    assert set(SCORE_PAYLOAD[0]) != set(COURSE_SCHEDULE_PAYLOAD[0])
    assert set(SCORE_PAYLOAD[0]) != set(EXAM_SCHEDULE_PAYLOAD[0])


def test_base_url_trailing_slash_does_not_double_up() -> None:
    """基址尾部的斜杠不会拼出 ``//api/scores``。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(
        payload=SCORE_PAYLOAD, base_url="http://campus.test/", requests=requests
    )

    client.fetch_scores()

    assert requests[0].url.path == SCORE_PATH


# ============================================================ 参数透传


def test_configured_student_id_is_the_default_query_value() -> None:
    """未显式传学号时，用构造时配置的当前学生。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(
        payload=SCORE_PAYLOAD, default_student_id="student-777", requests=requests
    )

    client.fetch_scores()

    assert dict(requests[0].url.params) == {"studentId": "student-777"}


def test_explicit_student_id_overrides_the_default() -> None:
    """显式传入的学号优先于配置默认值。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=SCORE_PAYLOAD, requests=requests)

    client.fetch_scores(student_id="student-042")

    assert dict(requests[0].url.params) == {"studentId": "student-042"}


def test_semester_is_sent_as_a_query_parameter() -> None:
    """``semester`` 以 query 参数形式发出，且与 ``studentId`` 同时存在。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=SCORE_PAYLOAD, requests=requests)

    client.fetch_scores(semester=CURRENT_SEMESTER)

    assert dict(requests[0].url.params) == {
        "studentId": STUDENT_ID,
        "semester": CURRENT_SEMESTER,
    }


def test_omitting_semester_keeps_the_query_minimal() -> None:
    """不传 ``semester`` 时不带该参数（服务端据此返回全部成绩）。

    显式传 ``semester=None`` 与省略参数必须等价：客户端不该发出 ``semester=``
    这样的空值，否则服务端要额外处理「空字符串 vs 缺参」两种形态。
    """

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=SCORE_PAYLOAD, requests=requests)

    client.fetch_scores()
    client.fetch_scores(semester=None)

    assert [dict(request.url.params) for request in requests] == [
        {"studentId": STUDENT_ID},
        {"studentId": STUDENT_ID},
    ]


@pytest.mark.parametrize(
    "semester", ["2025-2026-1", "2024-2025-2", "2023-2024-1"]
)
def test_semester_value_is_passed_through_verbatim(semester: str) -> None:
    """学期原样透传：客户端不解析、不规范化、不判断「这个学期存不存在」。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=SCORE_PAYLOAD, requests=requests)

    client.fetch_scores(semester=semester)

    assert requests[0].url.params["semester"] == semester


def test_semester_filtering_happens_on_the_server_not_in_the_client() -> None:
    """过滤在服务端：客户端只透传条件，返回什么由服务响应决定。

    这条边界值得显式守住——如果哪天有人把「按学期过滤」搬到客户端，
    这里会出现「请求没带 semester、结果却被过滤过」的矛盾，用例立刻失败。
    """

    requests: list[httpx.Request] = []
    client = make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS, requests=requests)

    rows = client.fetch_scores(semester=CURRENT_SEMESTER)

    # 1) 条件确实发出去了
    assert dict(requests[0].url.params) == {
        "studentId": STUDENT_ID,
        "semester": CURRENT_SEMESTER,
    }
    # 2) 返回内容与「服务端按同一条件过滤」的结果一致
    assert rows == score_payload(semester=CURRENT_SEMESTER)
    # 3) 上学期才有的课程不在本学期（过滤真的发生了，而不是原样全返）
    assert "高等数学" not in {row["course"] for row in rows}
    assert len(rows) < len(SCORE_PAYLOAD)


def test_empty_result_is_not_an_error() -> None:
    """该学生（或该学期）没有成绩时返回空列表：这是正常结果，不是异常。"""

    assert make_campus_client(payload=[]).fetch_scores() == []
    assert make_campus_client(payload=[]).fetch_scores(semester=CURRENT_SEMESTER) == []


# ============================================================ 失败路径分类


def test_timeout_becomes_unavailable() -> None:
    """请求超时 -> 服务不可用（可重试语义）。"""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("读取超时", request=request)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        make_campus_client(timeout_handler).fetch_scores()

    assert "超时" in str(excinfo.value)


def test_connection_failure_becomes_unavailable() -> None:
    """连接被拒（服务没启动）-> 服务不可用。"""

    def refused_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        make_campus_client(refused_handler).fetch_scores(semester=CURRENT_SEMESTER)

    assert "暂时不可用" in str(excinfo.value)


@pytest.mark.parametrize("status_code", [400, 401, 404, 422])
def test_http_4xx_becomes_request_error(status_code: int) -> None:
    """4xx -> 请求被拒绝（参数/权限语义）。"""

    with pytest.raises(CampusServiceRequestError) as excinfo:
        make_campus_client(status_code=status_code).fetch_scores()

    assert str(status_code) in str(excinfo.value)


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_http_5xx_becomes_server_error(status_code: int) -> None:
    """5xx -> 服务端错误（模型应表述为「稍后再试」）。"""

    with pytest.raises(CampusServiceServerError) as excinfo:
        make_campus_client(status_code=status_code).fetch_scores()

    assert str(status_code) in str(excinfo.value)


def test_invalid_json_becomes_data_error() -> None:
    """返回体不是 JSON（例如被反向代理换成 HTML 错误页）-> 格式异常。"""

    with pytest.raises(CampusServiceDataError) as excinfo:
        make_campus_client(raw_body="<html>502 Bad Gateway</html>").fetch_scores()

    assert "格式异常" in str(excinfo.value)


def test_non_array_payload_becomes_data_error() -> None:
    """JSON 合法但不是数组 -> 格式异常。"""

    with pytest.raises(CampusServiceDataError):
        make_campus_client(payload={"semester": CURRENT_SEMESTER}).fetch_scores()


def test_non_object_item_becomes_data_error() -> None:
    """数组元素不是对象 -> 格式异常，并指出是第几条。"""

    with pytest.raises(CampusServiceDataError) as excinfo:
        make_campus_client(payload=["92.0"]).fetch_scores(semester=CURRENT_SEMESTER)

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
_SCORE_FAILURE_BUILDERS: dict[str, Callable[[], CampusApiClient]] = {
    "unavailable": lambda: make_campus_client(_refused_handler),
    "timeout": lambda: make_campus_client(_timeout_handler),
    "http-4xx": lambda: make_campus_client(status_code=400),
    "http-5xx": lambda: make_campus_client(status_code=500),
    "invalid-json": lambda: make_campus_client(raw_body="<html>502 Bad Gateway</html>"),
    "non-array": lambda: make_campus_client(payload={"unexpected": 1}),
    "non-object-item": lambda: make_campus_client(payload=["92.0"]),
}


@pytest.mark.parametrize("label", sorted(_SCORE_FAILURE_BUILDERS))
def test_failure_messages_are_user_facing(label: str) -> None:
    """失败消息必须是「可转述给用户的中文」，不含 URL 与堆栈。"""

    client = _SCORE_FAILURE_BUILDERS[label]()

    with pytest.raises(CampusApiError) as excinfo:
        client.fetch_scores(semester=CURRENT_SEMESTER)

    message = str(excinfo.value)
    assert message.endswith("。"), f"{label}: {message}"
    for token in _LEAKY_TOKENS:
        assert token not in message, f"{label} 的异常消息泄漏了技术细节 {token!r}: {message}"
    assert "校园业务" in message


def test_scores_and_exams_share_the_same_exception_family() -> None:
    """接口不同、异常体系相同：调用方无需为成绩单独写一套失败分支。"""

    with pytest.raises(CampusServiceUnavailable):
        make_campus_client(_refused_handler).fetch_scores()

    with pytest.raises(CampusServiceUnavailable):
        make_campus_client(_refused_handler).fetch_exam_schedule()
