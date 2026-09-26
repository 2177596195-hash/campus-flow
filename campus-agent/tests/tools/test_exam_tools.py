"""考试安排工具测试：``get_exam_schedule`` → ``CampusApiClient`` → 替身服务。

关注三件事：

1. **结构转换**：HTTP 响应被整理成对外契约的四个字符串字段；
2. **不再依赖本地固定数据**：换一份服务响应，工具输出随之改变（行为证明），
   同时源码里不存在任何考试数据字面量（结构证明）；
3. **失败可读**：五类服务端失败经工具原样上抛为可读异常，交由
   ``handle_tool_error`` 转成 ``ToolMessage``。

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
from app.tools import exam_tools
from app.tools.exam_tools import EXAM_FIELDS, get_exam_schedule
from tests._fakes import EXAM_SCHEDULE_PAYLOAD, make_campus_client, patch_campus_api

CONTRACT_FIELDS = {"course", "date", "time", "room"}

#: 旧实现里写死的示例数据。这些字面量不得再出现在工具源码中，
#: 否则意味着「数据又回到了 Python 里」。
LEGACY_MOCK_LITERALS = (
    "计算机网络",
    "数据结构",
    "A302",
    "B201",
    "2026-09-28",
    "2026-09-30",
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


def _import_lines(source: str) -> list[str]:
    """取出源码中的 import 行（与检索层的架构守卫同一套做法）。"""

    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


# ============================================================ 结构转换


def test_tool_returns_contract_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具输出即对外契约：四个字段、全部字符串。"""

    patch_campus_api(monkeypatch, make_campus_client())

    rows = get_exam_schedule.invoke({})

    assert rows == EXAM_SCHEDULE_PAYLOAD
    assert len(rows) == 2
    for row in rows:
        assert set(row) == CONTRACT_FIELDS
        assert all(isinstance(value, str) for value in row.values())


def test_contract_field_order_matches_schema() -> None:
    """``EXAM_FIELDS`` 声明了对外字段，避免「字段名散落多处」。"""

    assert EXAM_FIELDS == ("course", "date", "time", "room")


def test_tool_strips_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """字段值两端空白被清掉：避免模型回答里出现多余空格。"""

    patch_campus_api(
        monkeypatch,
        make_campus_client(
            payload=[{"course": "  数据结构 ", "date": "2026-09-30", "time": "19:00 ", "room": " B201"}]
        ),
    )

    assert get_exam_schedule.invoke({}) == [
        {"course": "数据结构", "date": "2026-09-30", "time": "19:00", "room": "B201"}
    ]


def test_empty_service_result_yields_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """该学生没有考试时返回空列表，而不是报错。"""

    patch_campus_api(monkeypatch, make_campus_client(payload=[]))

    assert get_exam_schedule.invoke({}) == []


def test_tool_passes_configured_student_id_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """查询参数来自配置：工具自己不拼 URL、不决定学生身份。"""

    requests = []
    patch_campus_api(
        monkeypatch, make_campus_client(default_student_id="student-777", requests=requests)
    )

    get_exam_schedule.invoke({})

    assert dict(requests[0].url.params) == {"studentId": "student-777"}


# ================================================ 不再依赖本地 mock 数据


def test_tool_output_follows_the_service_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一份服务响应，工具输出随之改变。

    这比「断言返回值等于某常量」更能证明数据确实来自业务服务——
    若工具仍在返回本地固定数据，本用例必然失败。
    """

    patch_campus_api(
        monkeypatch,
        make_campus_client(
            payload=[{"course": "量子力学", "date": "2027-01-05", "time": "08:30", "room": "D101"}]
        ),
    )

    assert get_exam_schedule.invoke({}) == [
        {"course": "量子力学", "date": "2027-01-05", "time": "08:30", "room": "D101"}
    ]


def test_tool_source_contains_no_exam_data_literals() -> None:
    """结构证明：工具源码里不再有任何考试数据字面量。"""

    source = Path(exam_tools.__file__).read_text(encoding="utf-8")

    for literal in LEGACY_MOCK_LITERALS:
        assert literal not in source, f"工具源码中残留了写死的考试数据 {literal!r}"
    # 数据只能经由 API Client 取得
    assert "get_campus_api_client()" in source
    assert ".fetch_exam_schedule(" in source


def test_tool_does_not_touch_database_or_http_details() -> None:
    """依赖方向守卫：数据库访问与 HTTP 细节都不属于工具层。"""

    source = Path(exam_tools.__file__).read_text(encoding="utf-8")
    imports = " ".join(_import_lines(source))

    for forbidden in FORBIDDEN_TOOL_IMPORTS:
        assert forbidden not in imports, f"工具层不得直接依赖 {forbidden!r}"


def test_tool_declares_no_arguments_so_schema_stays_stable() -> None:
    """工具不接受参数：查询条件由配置与学生身份决定，模型无需也不应干预。

    保持这一点也让 ``get_exam_schedule`` 的 schema 与接真实服务之前完全一致。
    """

    assert get_exam_schedule.args == {}
    assert get_exam_schedule.args_schema.model_json_schema().get("properties", {}) == {}


def test_tool_description_mentions_the_real_data_source() -> None:
    """工具描述要告诉模型「数据是实时查来的」，降低它凭记忆编造的概率。"""

    description = get_exam_schedule.description

    assert "考试" in description
    assert "实时" in description
    for field_name in EXAM_FIELDS:
        assert field_name in description


# ============================================================ 失败路径


def test_missing_field_raises_readable_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少必填字段 → 格式异常，并指出第几条、缺哪个字段。"""

    patch_campus_api(
        monkeypatch,
        make_campus_client(payload=[{"course": "数据结构", "date": "2026-09-30", "time": "19:00"}]),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_exam_schedule.invoke({})

    message = str(excinfo.value)
    assert "第 1 条" in message
    assert "room" in message


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_field_raises_data_error(blank: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """字段存在但为空 / 为 null，同样视为格式异常。"""

    patch_campus_api(
        monkeypatch,
        make_campus_client(
            payload=[{"course": "数据结构", "date": "2026-09-30", "time": "19:00", "room": blank}]
        ),
    )

    with pytest.raises(CampusServiceDataError):
        get_exam_schedule.invoke({})


def test_second_record_error_reports_its_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误定位到具体记录：第 2 条缺字段时不应只报「格式异常」。"""

    patch_campus_api(
        monkeypatch,
        make_campus_client(
            payload=[
                {"course": "计算机网络", "date": "2026-09-28", "time": "14:00", "room": "A302"},
                {"course": "数据结构", "date": "2026-09-30", "time": "19:00"},
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_exam_schedule.invoke({})

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

    patch_campus_api(monkeypatch, _SERVICE_FAILURES[label]())

    with pytest.raises(CampusApiError) as excinfo:
        get_exam_schedule.invoke({})

    message = str(excinfo.value)
    assert "校园业务" in message
    assert "Traceback" not in message
    assert "http://" not in message


def test_expected_exception_types_per_failure_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败分类到位：4xx / 5xx / 格式异常 各归其类，调用方可分别处理。"""

    patch_campus_api(monkeypatch, make_campus_client(status_code=404))
    with pytest.raises(CampusServiceRequestError):
        get_exam_schedule.invoke({})

    patch_campus_api(monkeypatch, make_campus_client(status_code=503))
    with pytest.raises(CampusServiceServerError):
        get_exam_schedule.invoke({})

    patch_campus_api(monkeypatch, make_campus_client(raw_body="not json"))
    with pytest.raises(CampusServiceDataError):
        get_exam_schedule.invoke({})

    patch_campus_api(monkeypatch, make_campus_client(payload=[]))
    assert get_exam_schedule.invoke({}) == []  # 空结果不是失败


def test_connect_error_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务没启动（连接被拒）→ 服务不可用，文案提示「稍后再试」。"""

    patch_campus_api(monkeypatch, make_campus_client(_refused))

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        get_exam_schedule.invoke({})

    assert "暂时不可用" in str(excinfo.value)
