"""校园业务服务「请假提交」客户端测试（完全离线）。

这是 ``CampusApiClient`` 的**第一个写操作**，因此本文件除了沿用
``test_campus_api_scores.py`` 的替身策略（``httpx.MockTransport`` 替换
「数据包从哪里来」，客户端代码路径真实执行）之外，还要多守几件写操作独有的事：

1. **请求长什么样**：方法必须是 ``POST``、路径 ``/api/leaves``、请求体是 JSON 且
   字段名与 Spring Boot 的 DTO 一致（``studentId`` / ``startTime`` / ``endTime`` /
   ``reason``）；
2. **身份从哪来**：``student_id`` 不在用户意图里，省略时取构造时的默认学生，
   显式传入时以显式值优先——这条直接关系到「会不会替别人提交申请」；
3. **成功返回单条记录**（对象，不是数组），原样交给上层做字段归一化；
4. **失败分类**：4xx（带上服务端给出的原因）、5xx、超时、连接失败、非 JSON、
   返回结构不对，各自落到哪个异常，消息能否被模型直接转述给用户。

为什么「4xx 要带服务端原因」在这里比查询接口更要紧：提交被拒几乎总是
「传了不合法的内容」（时间格式不对、事由为空），把服务端的原因交给模型，
它才知道该改哪一项；查询接口的 4xx 主要是参数缺失，服务端没有额外信息可给。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.infrastructure.campus_api import (
    LEAVE_PATH,
    CampusApiClient,
    CampusApiError,
    CampusServiceDataError,
    CampusServiceRequestError,
    CampusServiceServerError,
    CampusServiceUnavailable,
)
from tests._fakes import (
    CAMPUS_TEST_BASE_URL,
    DEFAULT_CAMPUS_PAYLOADS,
    LEAVE_APPLICATION_PAYLOAD,
    make_campus_client,
)

STUDENT_ID = "student-001"

START = "2026-10-12T08:00"
END = "2026-10-14T18:00"
REASON = "感冒发烧需就医"


def submit(client: CampusApiClient, **overrides: Any) -> dict[str, Any]:
    """按测试默认值提交一条请假申请（可覆盖任意字段）。"""

    payload: dict[str, Any] = {"start_time": START, "end_time": END, "reason": REASON}
    payload.update(overrides)
    return client.submit_leave_application(**payload)


# ============================================================ 1. 请求形态


def test_successful_submission_posts_to_the_leaves_endpoint() -> None:
    """请求方法、路径与请求体都符合 Spring Boot 侧契约。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=LEAVE_APPLICATION_PAYLOAD, requests=requests)

    record = submit(client)

    assert len(requests) == 1
    request = requests[0]
    # 写操作的语义靠方法区分：GET 是查询，POST 是创建
    assert request.method == "POST"
    assert request.url.path == LEAVE_PATH  # /api/leaves
    assert str(request.url).startswith(CAMPUS_TEST_BASE_URL)
    # 写操作没有 query 参数，参数一律在请求体里
    assert dict(request.url.params) == {}
    assert record == LEAVE_APPLICATION_PAYLOAD


def test_request_body_uses_the_service_dto_field_names() -> None:
    """请求体字段名与 Spring Boot ``LeaveApplicationRequest`` 逐字一致。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(payload=LEAVE_APPLICATION_PAYLOAD, requests=requests)

    submit(client)

    body = _json_body(requests[0])
    # 驼峰命名与 Java 侧 record 组件名一致；改名会让服务端收到 null 并返回 400
    assert set(body) == {"studentId", "startTime", "endTime", "reason"}
    assert body["startTime"] == START
    assert body["endTime"] == END
    assert body["reason"] == REASON


def test_student_id_comes_from_configuration_and_can_be_overridden() -> None:
    """学号默认取构造时的当前学生；显式传入时以显式值优先。"""

    requests: list[httpx.Request] = []
    client = make_campus_client(
        payload=LEAVE_APPLICATION_PAYLOAD, requests=requests, default_student_id=STUDENT_ID
    )

    submit(client)
    submit(client, student_id="student-002")

    assert _json_body(requests[0])["studentId"] == STUDENT_ID
    assert _json_body(requests[1])["studentId"] == "student-002"


def test_leave_and_query_endpoints_do_not_mix() -> None:
    """按路径路由：请假提交打到 ``/api/leaves``，不会被查询接口的响应串台。"""

    client = make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS)

    record = submit(client)

    # 替身回显了请求体，因此这条断言同时证明「请求真的到了请假那条路由」
    assert record["startTime"] == START
    assert record["reason"] == REASON
    assert record["status"] == "PENDING"


# ============================================================ 2. 响应解析


def test_submission_returns_a_single_record_not_a_list() -> None:
    """提交接口返回**对象**；客户端原样返回，不做字段裁剪与业务解释。"""

    client = make_campus_client(payload=LEAVE_APPLICATION_PAYLOAD)

    record = submit(client)

    assert isinstance(record, dict)
    assert record["id"] == 1
    assert record["status"] == "PENDING"


@pytest.mark.parametrize("payload", [[], ["not-a-record"], "record", 42, None])
def test_non_object_response_is_rejected(payload: Any) -> None:
    """返回的不是对象时报数据格式错误——否则下游会以看不懂的方式崩掉。"""

    client = make_campus_client(payload=payload)

    with pytest.raises(CampusServiceDataError) as excinfo:
        submit(client)

    assert "格式异常" in str(excinfo.value)


# ============================================================ 3. 失败分类


@pytest.mark.parametrize("status_code", [400, 404, 422])
def test_client_error_includes_the_server_reason(status_code: int) -> None:
    """4xx 带上服务端给出的原因，并提示「检查提交内容」。"""

    client = make_campus_client(
        status_code=status_code, payload={"message": "请假结束时间不能早于开始时间。"}
    )

    with pytest.raises(CampusServiceRequestError) as excinfo:
        submit(client)

    message = str(excinfo.value)
    assert str(status_code) in message
    # 服务端的具体原因必须原样带上：模型靠它决定改哪一项
    assert "请假结束时间不能早于开始时间" in message
    # 写操作的纠正提示与查询不同
    assert "检查提交内容" in message


def test_client_error_without_a_message_still_degrades_gracefully() -> None:
    """服务端 4xx 没带 ``message`` 时退回通用文案，而不是再抛一个错。"""

    client = make_campus_client(status_code=400, payload={"error": "Bad Request"})

    with pytest.raises(CampusServiceRequestError) as excinfo:
        submit(client)

    assert "检查提交内容" in str(excinfo.value)


def test_server_error_is_classified_separately() -> None:
    """5xx 与 4xx 分开：前者该「稍后再试」，后者该「改参数」。"""

    client = make_campus_client(status_code=500, payload={"message": "内部错误"})

    with pytest.raises(CampusServiceServerError) as excinfo:
        submit(client)

    assert "稍后再试" in str(excinfo.value)


def test_timeout_is_reported_as_unavailable() -> None:
    """超时归为「暂时不可用」，而不是「提交被拒」。"""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("模拟超时", request=request)

    client = make_campus_client(timeout)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        submit(client)

    assert "超时" in str(excinfo.value)


def test_connection_failure_is_reported_as_unavailable() -> None:
    """连接被拒同样归为「暂时不可用」，且不把 URL 暴露给模型。"""

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    client = make_campus_client(refused)

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        submit(client)

    assert "暂时不可用" in str(excinfo.value)
    assert "http://" not in str(excinfo.value)


def test_non_json_response_is_reported_as_data_error() -> None:
    """响应不是合法 JSON 时报数据格式错误。"""

    client = make_campus_client(status_code=201, raw_body="<html>not json</html>")

    with pytest.raises(CampusServiceDataError) as excinfo:
        submit(client)

    assert "格式异常" in str(excinfo.value)


def test_every_failure_is_a_campus_api_error() -> None:
    """所有失败都是 ``CampusApiError`` 家族成员：``handle_tool_error`` 只认这一族。"""

    for handler in (
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom", request=request)),
        lambda request: httpx.Response(400, json={"message": "x"}, request=request),
        lambda request: httpx.Response(503, json={}, request=request),
        lambda request: httpx.Response(201, text="not json", request=request),
    ):
        client = make_campus_client(handler)  # type: ignore[arg-type]
        with pytest.raises(CampusApiError):
            submit(client)


# ============================================================ 4. 结构守卫


def test_leave_path_is_exported_for_reuse() -> None:
    """路径常量由基础设施层导出，其他层不得另写一份字面量。"""

    assert LEAVE_PATH == "/api/leaves"


def _json_body(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content.decode("utf-8"))
