"""成绩工具测试：``get_score`` -> ``CampusApiClient`` -> 替身服务。

关注四件事：

1. **结构转换**：HTTP 响应被整理成对外契约的四个字段，且 ``score`` / ``credit``
   保持**数值类型**（模型要直接比较大小与做加权）；
2. **不再依赖本地固定数据**：换一份服务响应，工具输出随之改变（行为证明），
   同时源码里不存在任何成绩数据字面量（结构证明）；
3. **参数语义**：``semester`` 透传成 query 参数（过滤在服务端），``course``
   是本地挑选、**不进请求**；
4. **失败可读**：五类服务端失败与数值字段非法都经工具上抛为可读异常，
   交由 ``handle_tool_error`` 转成 ``ToolMessage``，不中断整轮对话。

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
from app.tools import score_tools
from app.tools.score_contract import SCORE_FIELDS, normalize_score_row
from app.tools.score_tools import get_score
from tests._fakes import (
    DEFAULT_CAMPUS_PAYLOADS,
    SCORE_PAYLOAD,
    make_campus_client,
    patch_campus_api_in,
    score_payload,
)

CONTRACT_FIELDS = {"semester", "course", "score", "credit"}

CURRENT_SEMESTER = "2025-2026-1"
LAST_SEMESTER = "2024-2025-2"

#: 旧实现里写死的示例数据（课程名与分数）与旧实现的结构标识。
#: 这些字面量不得再出现在工具源码中，否则意味着「数据又回到了 Python 里」。
#:
#: 刻意**不含** ``CURRENT_SEMESTER`` 的取值：它是**教务日历常量**（与课程表工具的
#: ``CURRENT_WEEK`` 同理，用于让模型把「这个学期」映射成具体值），
#: 不是某一条成绩数据。把学期取值也列入守卫，会让「告诉模型当前学期」与
#: 「不得写死数据」这两件正确的事互相冲突。同理不含「形如 2025-2026-1」这类格式示例。
LEGACY_MOCK_LITERALS = (
    "_SCORE_TABLE",
    "KNOWN_SEMESTERS",
    "数据结构",
    "计算机网络",
    "操作系统",
    "高等数学",
    "大学英语",
    "88.0",
    "91.5",
    "79.0",
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
    """把替身注入 ``score_tools`` 自己的命名空间。"""

    return patch_campus_api_in(monkeypatch, score_tools, client)


def _import_lines(source: str) -> list[str]:
    """取出源码中的 import 行（与检索层的架构守卫同一套做法）。"""

    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


# ============================================================ 结构转换


def test_tool_returns_contract_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具输出即对外契约：四个字段。"""

    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD))

    rows = get_score.invoke({})

    assert rows == SCORE_PAYLOAD
    assert len(rows) == 5
    for row in rows:
        assert set(row) == CONTRACT_FIELDS


def test_contract_field_order_matches_schema() -> None:
    """``SCORE_FIELDS`` 声明了对外字段，避免「字段名散落多处」。"""

    assert SCORE_FIELDS == ("semester", "course", "score", "credit")


def test_contract_field_order_is_the_result_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """结果字典的键顺序与 ``SCORE_FIELDS`` 一致（跨语言契约靠顺序对齐）。"""

    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD))

    assert list(get_score.invoke({})[0]) == list(SCORE_FIELDS)


def test_numeric_fields_stay_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    """``score`` / ``credit`` 是数值，文本字段是字符串。"""

    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD))

    for row in get_score.invoke({}):
        assert isinstance(row["semester"], str)
        assert isinstance(row["course"], str)
        assert isinstance(row["score"], float)
        assert isinstance(row["credit"], float)


def test_numeric_field_accepts_json_integer_and_numeric_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数值字段容错：JSON 整数与数字字符串都归一化成 ``float``。

    服务端把 92 序列化成整数是合法的 JSON，不该被判成格式异常。
    """

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[{"semester": CURRENT_SEMESTER, "course": "某课", "score": 92, "credit": "4.0"}]
        ),
    )

    assert get_score.invoke({}) == [
        {"semester": CURRENT_SEMESTER, "course": "某课", "score": 92.0, "credit": 4.0}
    ]


def test_tool_preserves_server_side_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """排序由服务端完成，工具不重排：返回顺序即响应顺序。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"semester": CURRENT_SEMESTER, "course": "乙课", "score": 70.0, "credit": 1.0},
                {"semester": LAST_SEMESTER, "course": "甲课", "score": 80.0, "credit": 2.0},
            ]
        ),
    )

    rows = get_score.invoke({})

    assert [row["course"] for row in rows] == ["乙课", "甲课"]


def test_text_fields_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """文本字段两端空白被清掉：避免模型回答里出现多余空格。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[{"semester": " 2025-2026-1 ", "course": " 某课 ", "score": 92.0, "credit": 4.0}]
        ),
    )

    assert get_score.invoke({}) == [
        {"semester": CURRENT_SEMESTER, "course": "某课", "score": 92.0, "credit": 4.0}
    ]


def test_empty_service_result_yields_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """该学生（或该学期）没有成绩时返回空列表，而不是报错。"""

    _patch(monkeypatch, make_campus_client(payload=[]))

    assert get_score.invoke({}) == []
    assert get_score.invoke({"semester": CURRENT_SEMESTER}) == []


# ============================================================ 参数语义


def test_tool_passes_configured_student_id_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询参数来自配置：工具自己不拼 URL、不决定学生身份。"""

    requests: list[httpx.Request] = []
    _patch(
        monkeypatch,
        make_campus_client(
            payload=SCORE_PAYLOAD,
            default_student_id="student-777",
            requests=requests,
        ),
    )

    get_score.invoke({})

    assert dict(requests[0].url.params) == {"studentId": "student-777"}


def test_tool_forwards_semester_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """``semester`` 被原样送进查询条件：过滤由服务端完成。"""

    requests: list[httpx.Request] = []
    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD, requests=requests))

    get_score.invoke({"semester": CURRENT_SEMESTER})

    assert dict(requests[0].url.params) == {
        "studentId": "student-001",
        "semester": CURRENT_SEMESTER,
    }


def test_tool_omits_semester_when_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 ``semester`` 时不带该参数——由服务端返回全部成绩。"""

    requests: list[httpx.Request] = []
    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD, requests=requests))

    get_score.invoke({})

    assert dict(requests[0].url.params) == {"studentId": "student-001"}


def test_course_is_not_sent_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """``course`` 是**本地挑选**，不进请求。

    接口契约只按学号与学期筛选；把课程名匹配也塞进 query 会要求服务端再实现
    一套模糊匹配规则（第二份实现）。这里把这条边界显式守住：给定 ``course`` 时
    请求参数与不给定完全一致。
    """

    requests: list[httpx.Request] = []
    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD, requests=requests))

    get_score.invoke({"course": "某课"})

    assert dict(requests[0].url.params) == {"studentId": "student-001"}
    assert "course" not in requests[0].url.params


def test_tool_output_follows_the_semester_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一个学期，工具输出随之改变。

    这比「断言返回值等于某常量」更能证明 ``semester`` 真的到达了服务端——
    若工具把该参数丢掉，两个学期的结果会一模一样。
    """

    _patch(monkeypatch, make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS))

    current = get_score.invoke({"semester": CURRENT_SEMESTER})
    last = get_score.invoke({"semester": LAST_SEMESTER})

    assert current == score_payload(semester=CURRENT_SEMESTER)
    assert last == score_payload(semester=LAST_SEMESTER)
    assert current != last
    # 上学期才有的课程不出现在本学期，反之亦然
    assert "高等数学" not in {row["course"] for row in current}
    assert "数据结构" not in {row["course"] for row in last}


def test_course_filter_matches_bidirectionally(monkeypatch: pytest.MonkeyPatch) -> None:
    """课程名双向包含：模型补全成「某课课程」也能命中。"""

    _patch(monkeypatch, make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS))

    rows = get_score.invoke({"course": "数据结构"})
    assert [row["course"] for row in rows] == ["数据结构"]

    assert len(get_score.invoke({"course": "数据结构课程"})) == 1


def test_course_filter_returns_empty_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """查不到该课程时返回空列表，而不是报错。"""

    _patch(monkeypatch, make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS))

    assert get_score.invoke({"course": "量子力学导论"}) == []


def test_tool_keeps_its_optional_arguments_schema() -> None:
    """工具签名未变：仍是可选 ``semester`` / ``course``，学生身份不进 schema。"""

    schema = get_score.args_schema.model_json_schema()

    assert set(schema["properties"]) == {"semester", "course"}
    assert "semester" not in schema.get("required", [])
    assert "course" not in schema.get("required", [])
    assert "student_id" not in schema["properties"]
    assert "studentId" not in schema["properties"]


# ================================================ 不再依赖本地 mock 数据


def test_tool_output_follows_the_service_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """行为证明：换一份服务响应，工具输出随之改变。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"semester": "2023-2024-1", "course": "量子信息导论", "score": 77.5, "credit": 2.0}
            ]
        ),
    )

    assert get_score.invoke({}) == [
        {"semester": "2023-2024-1", "course": "量子信息导论", "score": 77.5, "credit": 2.0}
    ]


def test_tool_source_contains_no_score_data_literals() -> None:
    """结构证明：工具源码里不再有任何成绩数据字面量。"""

    source = Path(score_tools.__file__).read_text(encoding="utf-8")

    for literal in LEGACY_MOCK_LITERALS:
        assert literal not in source, f"工具源码中残留了写死的成绩数据 {literal!r}"
    # 数据只能经由 API Client 取得
    assert "get_campus_api_client()" in source
    assert ".fetch_scores(" in source


def test_tool_does_not_touch_database_or_http_details() -> None:
    """依赖方向守卫：数据库访问与 HTTP 细节都不属于工具层。"""

    source = Path(score_tools.__file__).read_text(encoding="utf-8")
    imports = " ".join(_import_lines(source))

    for forbidden in FORBIDDEN_TOOL_IMPORTS:
        assert forbidden not in imports, f"工具层不得直接依赖 {forbidden!r}"


def test_tool_description_mentions_the_real_data_source() -> None:
    """工具描述要告诉模型「数据是实时查来的」，降低它凭记忆编造的概率。"""

    description = get_score.description

    assert "成绩" in description
    assert "实时" in description
    for field_name in SCORE_FIELDS:
        assert field_name in description


# ============================================================ 失败路径


def test_missing_field_raises_readable_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少必填字段 -> 格式异常，并指出第几条、缺哪个字段。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[{"semester": CURRENT_SEMESTER, "course": "某课", "score": 92.0}]
        ),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_score.invoke({})

    message = str(excinfo.value)
    assert "第 1 条" in message
    assert "credit" in message


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_field_raises_data_error(
    blank: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字段存在但为空 / 为 null，同样视为格式异常。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"semester": CURRENT_SEMESTER, "course": blank, "score": 92.0, "credit": 4.0}
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError):
        get_score.invoke({})


def test_second_record_error_reports_its_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误定位到具体记录：第 2 条缺字段时不应只报「格式异常」。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"semester": CURRENT_SEMESTER, "course": "甲课", "score": 92.0, "credit": 4.0},
                {"semester": CURRENT_SEMESTER, "course": "乙课", "score": 88.0},
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_score.invoke({})

    assert "第 2 条" in str(excinfo.value)


def test_non_numeric_score_raises_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``score`` 不是数值（例如服务端误发字符串）-> 明确的格式异常。"""

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"semester": CURRENT_SEMESTER, "course": "某课", "score": "优秀", "credit": 4.0}
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError) as excinfo:
        get_score.invoke({})

    message = str(excinfo.value)
    assert "score" in message
    assert "第 1 条" in message


def test_boolean_score_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``true`` / ``false`` 不是成绩：bool 是 int 的子类，必须显式拒绝。

    否则 ``true`` 会被静默当成 1.0 分，把一条错误数据伪装成正常成绩。
    """

    _patch(
        monkeypatch,
        make_campus_client(
            payload=[
                {"semester": CURRENT_SEMESTER, "course": "某课", "score": True, "credit": 4.0}
            ]
        ),
    )

    with pytest.raises(CampusServiceDataError):
        get_score.invoke({})


def test_contract_rejects_non_finite_numbers() -> None:
    """``NaN`` / 无穷大无法参与有意义的大小比较，直接判为格式异常。

    直接测契约函数：``NaN`` 不是合法 JSON 数字，经 HTTP 构造反而绕远。
    """

    base = {"semester": CURRENT_SEMESTER, "course": "某课", "credit": 4.0}

    with pytest.raises(CampusServiceDataError):
        normalize_score_row({**base, "score": float("nan")}, index=1)

    with pytest.raises(CampusServiceDataError):
        normalize_score_row({**base, "score": float("inf")}, index=1)

    # 正常数值照常通过（对照组，避免用例因别的原因而「通过」）
    assert normalize_score_row({**base, "score": 92.0}, index=1)["score"] == 92.0


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
        get_score.invoke({"semester": CURRENT_SEMESTER})

    message = str(excinfo.value)
    assert "校园业务" in message
    assert "Traceback" not in message
    assert "http://" not in message


def test_expected_exception_types_per_failure_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败分类到位：4xx / 5xx / 格式异常 各归其类，调用方可分别处理。"""

    _patch(monkeypatch, make_campus_client(status_code=400))
    with pytest.raises(CampusServiceRequestError):
        get_score.invoke({})

    _patch(monkeypatch, make_campus_client(status_code=503))
    with pytest.raises(CampusServiceServerError):
        get_score.invoke({})

    _patch(monkeypatch, make_campus_client(raw_body="not json"))
    with pytest.raises(CampusServiceDataError):
        get_score.invoke({})

    _patch(monkeypatch, make_campus_client(payload=[]))
    assert get_score.invoke({}) == []  # 空结果不是失败


def test_connect_error_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务没启动（连接被拒）-> 服务不可用，文案提示「稍后再试」。"""

    _patch(monkeypatch, make_campus_client(_refused))

    with pytest.raises(CampusServiceUnavailable) as excinfo:
        get_score.invoke({})

    assert "暂时不可用" in str(excinfo.value)


def test_non_string_semester_is_rejected_before_any_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法参数在本地就被拒：不做一次注定失败的往返请求。

    经 ``ToolNode`` 调用时 ``args_schema`` 先行把住类型；这里直接调用函数本体，
    验证第二道防线同样生效。
    """

    requests: list[httpx.Request] = []
    _patch(monkeypatch, make_campus_client(payload=SCORE_PAYLOAD, requests=requests))

    with pytest.raises(TypeError):
        get_score.func(semester=2025)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        get_score.func(course=2025)  # type: ignore[arg-type]

    assert requests == []
