"""请假契约（``app.tools.leave_contract``）用例：入参归一化与结果字段校验。

为什么单独测这一层：请假是这条链路上**唯一由模型填写业务参数**的写操作，
参数写法千变万化（``2026-10-12 08:00`` / ``2026-10-12T08:00`` / 只给日期），
而下游只接受一种形态。把这些差异收敛在契约里、并由这里逐条断言，
MCP Server 与（将来的）直连工具才能表现一致——「模型怎么写」只有一处解释。

三类断言：

1. **入参归一化**：合法写法被统一成 ``YYYY-MM-DDTHH:MM``，非法写法给出可读原因；
2. **结果字段校验**：服务端字段缺失 / 为空时明确失败，而不是把半成品数据透传；
3. **契约边界**：本模块刻意不依赖 LangChain / MCP（写能力只经 MCP 暴露，
   MCP Server 进程里不该为了这份契约把 LangChain 拖进来）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.campus_api import CampusServiceDataError
from app.tools import leave_contract as contract_module
from app.tools.leave_contract import (
    LEAVE_RESULT_FIELDS,
    STATUS_PENDING,
    STATUS_PENDING_LABEL,
    normalize_datetime,
    normalize_leave_result,
    normalize_required_text,
)


# ============================================================ 1. 入参归一化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-12 08:00", "2026-10-12T08:00"),
        ("2026-10-12T08:00", "2026-10-12T08:00"),
        ("2026-10-12T08:00:00", "2026-10-12T08:00"),
        ("2026-10-12", "2026-10-12T00:00"),
        ("  2026-10-12 08:00  ", "2026-10-12T08:00"),
    ],
)
def test_normalize_datetime_unifies_the_accepted_shapes(raw: str, expected: str) -> None:
    """几种常见写法被统一成分钟精度的 ISO 8601。"""

    assert normalize_datetime(raw, name="请假开始时间") == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_normalize_datetime_treats_blank_as_missing(raw: object) -> None:
    """缺失与空白都算「没给」，错误消息里指出该给什么格式。"""

    with pytest.raises(ValueError) as excinfo:
        normalize_datetime(raw, name="请假开始时间")

    message = str(excinfo.value)
    assert "请假开始时间" in message
    assert "2026-10-12 08:00" in message  # 提示正确格式，模型据此改参


@pytest.mark.parametrize("raw", ["明天早上", "2026/10/12 08:00", "十点", "08:00"])
def test_normalize_datetime_rejects_unparsable_values(raw: str) -> None:
    """无法识别时给出可读中文，而不是抛底层解析器的英文异常。"""

    with pytest.raises(ValueError) as excinfo:
        normalize_datetime(raw, name="请假开始时间")

    assert "格式无法识别" in str(excinfo.value)


def test_normalize_datetime_rejects_non_string_values() -> None:
    """类型不符是调用方的问题（``TypeError``），与「上游服务数据坏了」区分开。"""

    with pytest.raises(TypeError):
        normalize_datetime(20261012, name="请假开始时间")


def test_normalize_required_text_strips_and_rejects_blank() -> None:
    """必填文本去空白；空串 / 纯空白视为缺失。"""

    assert normalize_required_text("  感冒发烧  ", name="请假事由") == "感冒发烧"

    for blank in (None, "", "   ", "\n"):
        with pytest.raises(ValueError) as excinfo:
            normalize_required_text(blank, name="请假事由")
        assert "请假事由" in str(excinfo.value)


def test_normalize_required_text_rejects_non_string_values() -> None:
    with pytest.raises(TypeError):
        normalize_required_text(123, name="请假事由")


# ============================================================ 2. 结果字段校验


def test_normalize_leave_result_keeps_the_contract_fields() -> None:
    """合法响应被整理成契约规定的字段（顺序即契约顺序）。"""

    row = {
        "id": 7,
        "studentId": "student-001",
        "startTime": "2026-10-12T08:00:00",
        "endTime": "2026-10-14T18:00:00",
        "reason": "感冒发烧需就医",
        "status": "PENDING",
        "createdAt": "2026-10-12T08:03:00",
    }

    normalized = normalize_leave_result(row)

    assert tuple(normalized) == LEAVE_RESULT_FIELDS
    # 全部字符串化：它们是要被复述与记录的标识，不参与计算
    assert all(isinstance(value, str) for value in normalized.values())
    assert normalized["id"] == "7"
    assert normalized["status"] == STATUS_PENDING


def test_normalize_leave_result_tolerates_extra_fields() -> None:
    """服务端多返回字段不影响结果：只挑契约里那七个。"""

    normalized = normalize_leave_result(
        {
            "id": 1,
            "studentId": "student-001",
            "startTime": "2026-10-12T08:00",
            "endTime": "2026-10-12T18:00",
            "reason": "事由",
            "status": "PENDING",
            "createdAt": "2026-10-12T08:03",
            "extraInternal": "不该出现在结果里",
        }
    )

    assert "extraInternal" not in normalized


@pytest.mark.parametrize("missing", LEAVE_RESULT_FIELDS)
def test_normalize_leave_result_fails_when_a_field_is_missing(missing: str) -> None:
    """缺任一字段即明确失败，错误消息里带上字段名，便于定位。"""

    row = {
        "id": 1,
        "studentId": "student-001",
        "startTime": "2026-10-12T08:00",
        "endTime": "2026-10-12T18:00",
        "reason": "事由",
        "status": "PENDING",
        "createdAt": "2026-10-12T08:03",
    }
    del row[missing]

    with pytest.raises(CampusServiceDataError) as excinfo:
        normalize_leave_result(row)

    assert missing in str(excinfo.value)


def test_normalize_leave_result_rejects_non_mapping_payload() -> None:
    with pytest.raises(CampusServiceDataError):
        normalize_leave_result(["not", "a", "record"])  # type: ignore[arg-type]


# ============================================================ 3. 契约边界


def test_contract_labels_are_single_sourced() -> None:
    """「待审核」这类文案只有一处，避免工具描述与用例各写一份。"""

    assert STATUS_PENDING == "PENDING"
    assert STATUS_PENDING_LABEL == "待审核"


def test_contract_module_does_not_depend_on_langchain_or_mcp() -> None:
    """契约模块不得依赖 LangChain / MCP：MCP Server 进程不该被拖进 LangChain。"""

    source = Path(contract_module.__file__).read_text(encoding="utf-8")
    import_lines = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]

    assert import_lines, "未扫描到 import 行，守卫用例失效"
    for line in import_lines:
        for token in ("langchain", "langgraph", "mcp", "pydantic"):
            assert token not in line.lower(), f"leave_contract 出现越界依赖: {line.strip()}"


def test_contract_module_has_no_leave_data_literals() -> None:
    """契约里不得出现真实业务数据（数据只在 MySQL 里）。

    与考试 / 课程 / 成绩三份契约的守卫同一套做法：举例一律用中性示例
    （``2026-10-12 08:00``），不取 MySQL 种子里的真实考试日期，
    免得「数据只在数据库」这条约定被描述文本悄悄削弱。
    """

    source = Path(contract_module.__file__).read_text(encoding="utf-8")

    for literal in (
        "计算机网络",
        "数据结构",
        "A302",
        "2026-09-28",  # MySQL 种子里的考试日期
        "2026-09-30",
        "感冒发烧",
        "student-001",
    ):
        assert literal not in source, f"leave_contract 出现业务数据字面量 {literal!r}"
