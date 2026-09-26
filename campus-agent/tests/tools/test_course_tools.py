"""课程表工具测试：``get_course_schedule`` -> ``CampusApiClient`` -> 替身服务。

关注四件事：

1. **结构转换**：HTTP 响应被整理成对外契约的六个字符串字段；
2. **不再依赖本地固定数据**：换一份服务响应，工具输出随之改变（行为证明），
   同时源码里不存在任何课程数据字面量（结构证明）；
3. **参数透传**：``week`` 原样作为查询条件发出，且不传时不带该参数；
4. **失败可读**：五类服务端失败经工具原样上抛为可读异常，交由
   ``handle_tool_error`` 转成 ``ToolMessage``，不中断整轮对话。

用 ``tests/conftest.py`` 的全局夹具保证离线；本文件按需再注入特定响应的替身。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.infrastructure.campus_api import (
    CampusApiClient,
    CampusApiError,
    CampusServiceDataError,
    CampusServiceRequestError,
    CampusServiceServerError,
    CampusServiceUnavailable,
)
from app.tools import course_tools
from app.tools.course_contract import COURSE_FIELDS
from app.tools.course_tools import get_course_schedule
from tests._fakes import (
    COURSE_SCHEDULE_PAYLOAD,
    DEFAULT_CAMPUS_PAYLOADS,
    TOOL_MODULES_WITH_CAMPUS_CLIENT,
    course_schedule_payload,
    make_campus_client,
    patch_campus_api_in,
)

CONTRACT_FIELDS = {"course", "teacher", "weekday", "time", "room", "weeks"}

#: 旧实现里写死的示例数据（课程名 / 教师 / 教室 / 表变量名）。
#: 这些字面量不得再出现在工具源码中，否则意味着「数据又回到了 Python 里」。
#:
#: 刻意**不**含「1-16周」这类周次渲染串：它是字段格式的示例（工具描述里会用
#: 「形如 1-16周」说明输出长什么样），不是某一条课程数据。把格式示例也列入
#: 守卫会让「描述清楚输出结构」与「不得写死数据」这两件正确的事互相冲突。
LEGACY_MOCK_LITERALS = (
    "高等数学",
    "Java程序设计",
    "计算机网络",
    "数据结构",
    "数据库原理",
    "操作系统",
    "李明",
    "王芳",
    "A302",
    "B201",
    "C105",
    "week_start",
    "week_end",
    "_COURSE_TABLE",
)

#: 工具层禁止直接依赖的东西：数据库驱动、HTTP 细节、RAG 底层
FORBIDDEN_TOOL_IMPORTS = (
    "mysql",
    "pymysql",
    "sqlalchemy",
    "sqlite3",
    "psycopg",
    "httpx",
    "requests",
    "chromadb",
    "app.rag.vectorstore",
    "app.rag.embeddings",
    "app.rag.retriever",
    "app.rag.manifest",
)


def _patch(monkeypatch: pytest.MonkeyPatch, client: CampusApiClient) -> CampusApiClient:
    """把替身注入 ``course_tools`` 自己的命名空间。"""

    return patch_campus_api_in(monkeypatch, course_tools, client)


def _import_lines(source: str) -> list[str]:
    """取出源码中的 import 行（与检索层的架构守卫同一套做法）。"""

    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


# ============================================================ 结构转换


def test_tool_returns_contract_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具输出即对外契约：六个字段、全部字符串。"""

    _patch(monkeypatch, make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD))

    rows = get_course_schedule.invoke({})

    assert rows == COURSE_SCHEDULE_PAYLOAD
    assert len(rows) == 8
    for row in rows:
        assert set(row) == CONTRACT_FIELDS
        assert all(isinstance(value, str) for value in row.values())


def test_contract_field_order_matches_schema() -> None:
    """``COURSE_FIELDS`` 声明了对外字段，避免「字段名散落多处」。"""

    assert COURSE_FIELDS == ("course", "teacher", "weekday", "time", "room", "weeks")


def test_tool_preserves_server_side_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """排序由服务端完成，工具不重排：返回顺序即响应顺序。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"course": "甲课", "teacher": "甲师", "weekday": "周五",
                 "time": "18:00-19:40", "room": "Z1", "weeks": "1-16周"},
                {"course": "乙课", "teacher": "乙师", "weekday": "周一",
                 "time": "08:00-09:40", "room": "Z2", "weeks": "1-16周"},
            ]
        ),
    )

    rows = get_course_schedule.invoke({})

    assert [row["course"] for row in rows] == ["甲课", "乙课"]


def test_tool_strips_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """字段值两端空白被清掉：避免模型回答里出现多余空格。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"course": " 数据结构 ", "teacher": " 李明", "weekday": "周一 ",
                 "time": " 08:00-09:40", "room": "A302 ", "weeks": " 1-16周"}
            ]
        ),
    )

    assert get_course_schedule.invoke({}) == [
        {"course": "数据结构", "teacher": "李明", "weekday": "周一",
         "time": "08:00-09:40", "room": "A302", "weeks": "1-16周"}
    ]


def test_empty_service_result_yields_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """该学生（或该周）没有课时返回空列表，而不是报错。"""

    _patch(monkeypatch, make_campus_client(payload=[]))

    assert get_course_schedule.invoke({}) == []
    assert get_course_schedule.invoke({"week": 3}) == []


# ============================================================ 参数透传


def test_tool_passes_configured_student_id_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询参数来自配置：工具自己不拼 URL、不决定学生身份。"""

    requests: list[httpx.Request] = []
    _patch(
        monkeypatch,
        make_campus_client(
            payload=COURSE_SCHEDULE_PAYLOAD,
            default_student_id="student-777",
            requests=requests,
        ),
    )

    get_course_schedule.invoke({})

    assert dict(requests[0].url.params) == {"studentId": "student-777"}


def test_tool_forwards_week_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """``week`` 被原样送进查询条件：模型说的第 3 周，服务端收到的就是 3。"""

    requests: list[httpx.Request] = []
    _patch(
        monkeypatch,
        make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests),
    )

    get_course_schedule.invoke({"week": 3})

    assert dict(requests[0].url.params) == {"studentId": "student-001", "week": "3"}


def test_tool_omits_week_when_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 ``week`` 时不带该参数——由服务端返回整学期课表。"""

    requests: list[httpx.Request] = []
    _patch(
        monkeypatch,
        make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests),
    )

    get_course_schedule.invoke({})

    assert dict(requests[0].url.params) == {"studentId": "student-001"}


def test_tool_output_follows_the_week_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一个周次，工具输出随之改变。

    这比「断言返回值等于某常量」更能证明 ``week`` 真的到达了服务端——
    若工具把该参数丢掉，第 3 周与第 8 周的结果会一模一样。
    """

    _patch(monkeypatch, make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS))

    week3 = get_course_schedule.invoke({"week": 3})
    week8 = get_course_schedule.invoke({"week": 8})

    assert week3 == course_schedule_payload(week=3)
    assert week8 == course_schedule_payload(week=8)
    assert week3 != week8
    # 数据库原理第 5 周才开课：第 3 周查不到、第 8 周查得到
    assert "数据库原理" not in {row["course"] for row in week3}
    assert "数据库原理" in {row["course"] for row in week8}


def test_tool_keeps_its_optional_week_argument_schema() -> None:
    """工具签名未变：仍是可选 ``week``，学生身份不进 schema。"""

    schema = get_course_schedule.args_schema.model_json_schema()

    assert set(schema["properties"]) == {"week"}
    assert "week" not in schema.get("required", [])
    assert "student_id" not in schema["properties"]
    assert "studentId" not in schema["properties"]


# ================================================ 不再依赖本地 mock 数据


def test_tool_output_follows_the_service_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一份服务响应，工具输出随之改变。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"course": "量子信息导论", "teacher": "钱老师", "weekday": "周六",
                 "time": "10:00-11:40", "room": "D101", "weeks": "3-9周"}
            ]
        ),
    )

    assert get_course_schedule.invoke({}) == [
        {"course": "量子信息导论", "teacher": "钱老师", "weekday": "周六",
         "time": "10:00-11:40", "room": "D101", "weeks": "3-9周"}
    ]


def test_tool_source_contains_no_course_data_literals() -> None:
    """结构证明：工具源码里不再有任何课程数据字面量。"""

    source = Path(course_tools.__file__).read_text(encoding="utf-8")

    for literal in LEGACY_MOCK_LITERALS:
        assert literal not in source, f"工具源码中残留了写死的课程数据 {literal!r}"
    # 数据只能经由 API Client 取得
    assert "get_campus_api_client()" in source
    assert ".fetch_course_schedule(" in source


def test_tool_does_not_touch_database_or_http_details() -> None:
    """依赖方向守卫：数据库访问与 HTTP 细节都不属于工具层。"""

    source = Path(course_tools.__file__).read_text(encoding="utf-8")
    imports = " ".join(_import_lines(source))

    for forbidden in FORBIDDEN_TOOL_IMPORTS:
        assert forbidden not in imports, f"工具层不得直接依赖 {forbidden!r}"


def test_tool_description_mentions_the_real_data_source() -> None:
    """工具描述要告诉模型「数据是实时查来的」，降低它凭记忆编造的概率。"""

    description = get_course_schedule.description

    assert "课" in description
    assert "实时" in description
    for field_name in COURSE_FIELDS:
        assert field_name in description


# ============================================================ 失败路径


def test_missing_field_raises_readable_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少必填字段 -> 格式异常，并指出第几条、缺哪个字段。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"course": "数据结构", "teacher": "李明", "weekday": "周一",
                 "time": "08:00-09:40", "room": "A302"}
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_course_schedule.invoke({})

    message = str(excinfo.value)
    assert "第 1 条" in message
    assert "weeks" in message


def test_second_record_error_reports_its_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误定位到具体记录：第 2 条缺字段时不应只报「格式异常」。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"course": "数据结构", "teacher": "李明", "weekday": "周一",
                 "time": "08:00-09:40", "room": "A302", "weeks": "1-16周"},
                {"course": "计算机网络", "teacher": "王芳", "weekday": "周一",
                 "time": "14:00-15:40", "room": "B201"},
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_course_schedule.invoke({})

    assert "第 2 条" in str(excinfo.value)


def _refused(request: httpx.Request) -> httpx.Response:
    """模拟服务未启动。"""

    raise httpx.ConnectError("Connection refused", request=request)


_SERVICE_FAILURES: dict[str, Callable[[], CampusApiClient]] = {
    "unavailable": lambda: make_campus_client(_refused),
    "http-4xx": lambda: make_campus_client(status_code=404),
    "http-5xx": lambda: make_campus_client(status_code=503),
    "invalid-json": lambda: make_campus_client(raw_body="<html>oops</html>"),
    "non-array": lambda: make_campus_client(payload={"unexpected": 1}),
}


@pytest.mark.parametrize("label", sorted(_SERVICE_FAILURES))
def test_service_failures_propagate_as_readable_errors(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """五类服务端失败经工具上抛，类型与文案都对模型可理解。"""

    _patch(monkeypatch, _SERVICE_FAILURES[label]())

    with pytest.raises(CampusApiError) as excinfo:
        get_course_schedule.invoke({"week": 3})

    message = str(excinfo.value)
    assert "校园业务" in message
    assert "Traceback" not in message
    assert "http://" not in message


def test_expected_exception_types_per_failure_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败分类到位：4xx / 5xx / 格式异常 各归其类，调用方可分别处理。"""

    _patch(monkeypatch, make_campus_client(status_code=400))
    with pytest.raises(CampusServiceRequestError):
        get_course_schedule.invoke({})

    _patch(monkeypatch, make_campus_client(status_code=503))
    with pytest.raises(CampusServiceServerError):
        get_course_schedule.invoke({})

    _patch(monkeypatch, make_campus_client(raw_body="not json"))
    with pytest.raises(CampusServiceDataError):
        get_course_schedule.invoke({})

    _patch(monkeypatch, make_campus_client(payload=[]))
    assert get_course_schedule.invoke({}) == []  # 空结果不是失败


def test_connect_error_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务没启动（连接被拒）-> 服务不可用，文案提示「稍后再试」。"""

    _patch(monkeypatch, make_campus_client(_refused))

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        get_course_schedule.invoke({})

    assert "暂时不可用" in str(excinfo.value)


def test_invalid_week_is_rejected_before_any_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法周次在本地就被拒：不做一次注定失败的往返请求。"""

    requests: list[httpx.Request] = []
    _patch(
        monkeypatch,
        make_campus_client(payload=COURSE_SCHEDULE_PAYLOAD, requests=requests),
    )

    with pytest.raises(ValueError):
        get_course_schedule.invoke({"week": 99})

    assert requests == []


# ============================================================ 架构守卫


def test_every_tool_module_using_the_client_is_registered_for_patching() -> None:
    """守卫：用到 ``get_campus_api_client()`` 的工具模块必须登记在替身名单里。

    漏登记的后果不会报错——用例会悄悄变成「去连本机 8081 上真实运行的服务」，
    在没有服务的机器上失败、在本机恰好开着服务时假绿。
    """

    tools_dir = Path(course_tools.__file__).parent
    users = {
        f"app.tools.{path.stem}"
        for path in tools_dir.glob("*.py")
        if "get_campus_api_client()" in path.read_text(encoding="utf-8")
    }

    assert users, "未扫描到任何使用客户端的工具模块，守卫用例失效"
    assert users == set(TOOL_MODULES_WITH_CAMPUS_CLIENT), (
        "工具模块与替身名单不一致：新增/删除了调用 CampusApiClient 的模块，"
        "请同步 tests/_fakes.py 的 TOOL_MODULES_WITH_CAMPUS_CLIENT"
    )
