"""校园教务日历事实「单一来源」的回归测试（P2 收敛）。

背景：修复前「当前学期 / 当前教学周」硬编码散落多处——
``score_tools.CURRENT_SEMESTER``、``course_tools.CURRENT_WEEK``、
MCP Server 的工具描述（「当前为第 3 教学周」「当前为 2025-2026-1 学期」），
且取值均已过期（2026-09 实际应为 2026-2027-1）。收敛后：

- **唯一生产来源**是配置层 ``get_campus_calendar_settings()``
  （``CAMPUS_CURRENT_SEMESTER`` / ``CAMPUS_CURRENT_WEEK`` 可覆盖，默认
  ``2026-2027-1`` / ``3``）；
- 工具描述（直连与 MCP）不再携带具体学期 / 周次——描述是静态说明书，
  不该成为事实数据的第二份来源；
- 「本学期」的槽位解析从配置读取，行为不变。

用例分四组：配置层（默认 / 覆盖 / 非法取值）、行为级（改配置后解析结果
跟着变——证明读的是统一来源而非某处写死的常量）、描述级（无日历事实）、
守卫级（生产代码无日历常量赋值）。全部离线。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app
from app.agent.slots import current_semester, previous_semester, resolve_semester
from app.config.settings import (
    CampusCalendarSettings,
    ConfigurationError,
    get_campus_calendar_settings,
)
from app.mcp.server import (
    COURSE_TOOL_DESCRIPTION,
    SCORE_TOOL_DESCRIPTION,
    SEMESTER_DESCRIPTION,
    WEEK_DESCRIPTION,
)
from app.tools.course_tools import get_course_schedule
from app.tools.score_tools import get_score

#: 任务给定的当前日历事实（2026-09：2026-2027 学年第 1 学期、第 3 教学周）
EXPECTED_SEMESTER = "2026-2027-1"
EXPECTED_WEEK = 3

#: 「当前为第 X 教学周」「当前为 X 学期」这类**日历事实声明**的形状。
#: 注意：描述里「形如 2025-2026-1」的**格式示例**不算事实声明，允许存在。
_CURRENT_WEEK_CLAIM = re.compile(r"当前[为是]\s*第\s*\d+\s*教学周")
_CURRENT_SEMESTER_CLAIM = re.compile(r"当前[为是]\s*\d{4}-\d{4}-[12]\s*学期")

#: 生产代码里不得再出现 ``CURRENT_SEMESTER =`` / ``CURRENT_WEEK =`` 式的
#: 日历常量赋值（配置层的 dataclass 字段是小写的 ``current_*``，不会命中）。
_CALENDAR_CONSTANT_ASSIGNMENT = re.compile(r"^\s*CURRENT_(SEMESTER|WEEK)\s*[:=]", re.MULTILINE)

#: 生产代码根目录（campus-agent/app）
_APP_DIR = Path(app.__file__).resolve().parent


@pytest.fixture
def fresh_calendar(monkeypatch: pytest.MonkeyPatch):
    """隔离配置缓存与环境变量：用例内改配置立即生效，结束后还原。"""

    get_campus_calendar_settings.cache_clear()
    monkeypatch.delenv("CAMPUS_CURRENT_SEMESTER", raising=False)
    monkeypatch.delenv("CAMPUS_CURRENT_WEEK", raising=False)
    yield monkeypatch
    get_campus_calendar_settings.cache_clear()


# ============================================================ 1. 配置层

def test_calendar_defaults_match_the_current_academic_year(fresh_calendar) -> None:
    """默认值即当前教务日历：2026-2027-1 学期、第 3 教学周（任务给定）。"""

    settings = get_campus_calendar_settings()

    assert settings == CampusCalendarSettings(
        current_semester=EXPECTED_SEMESTER, current_week=EXPECTED_WEEK
    )


def test_calendar_env_overrides_are_read(fresh_calendar) -> None:
    """学期与周次都可被环境变量覆盖（学期切换只改配置，不改代码）。"""

    fresh_calendar.setenv("CAMPUS_CURRENT_SEMESTER", "1998-1999-2")
    fresh_calendar.setenv("CAMPUS_CURRENT_WEEK", "7")

    settings = get_campus_calendar_settings()

    assert settings == CampusCalendarSettings(current_semester="1998-1999-2", current_week=7)


@pytest.mark.parametrize(
    "raw",
    ["", "2025-2030-1", "2025-2026-3", "25-2026-1", "上学期"],
)
def test_malformed_semester_rejected_at_config_load(raw: str, fresh_calendar) -> None:
    """非法学期串在配置读取时即报错，不会流进「本学期」的解析结果。"""

    fresh_calendar.setenv("CAMPUS_CURRENT_SEMESTER", raw)

    with pytest.raises(ConfigurationError):
        get_campus_calendar_settings()


@pytest.mark.parametrize("raw", ["0", "-3", "abc"])
def test_malformed_week_rejected_at_config_load(raw: str, fresh_calendar) -> None:
    """非法教学周同样在配置读取时即报错。"""

    fresh_calendar.setenv("CAMPUS_CURRENT_WEEK", raw)

    with pytest.raises(ConfigurationError):
        get_campus_calendar_settings()


# ============================================================ 2. 行为级：单一来源被真实消费

def test_current_semester_resolution_follows_the_config(fresh_calendar) -> None:
    """「本学期」解析实际跟随配置：改配置、结果跟着变（不是写死的常量）。

    这是成绩查询链路对统一来源的**行为级**验证：多轮槽位补齐里
    ``resolve_semester("本学期")`` 的返回值会被注入任务上下文并作为
    ``get_score`` 的 ``semester`` 参数，因此它读到哪里，工具就查到哪个学期。
    """

    fresh_calendar.setenv("CAMPUS_CURRENT_SEMESTER", "2030-2031-2")

    assert current_semester() == "2030-2031-2"
    assert resolve_semester("本学期") == "2030-2031-2"
    assert resolve_semester("这个学期的成绩出来了吗") == "2030-2031-2"
    # 「上学期」相对配置的当前学期推导，同样跟随配置
    assert resolve_semester("上学期") == previous_semester("2030-2031-2") == "2030-2031-1"


def test_semester_resolution_defaults_match_the_current_calendar(fresh_calendar) -> None:
    """默认配置下「本学期 / 上学期」的解析结果与配置一致（既有行为不变）。"""

    assert resolve_semester("本学期") == get_campus_calendar_settings().current_semester
    assert resolve_semester("上学期") == previous_semester(
        get_campus_calendar_settings().current_semester
    )


# ============================================================ 3. 描述级：工具描述不再携带日历事实


@pytest.mark.parametrize(
    "description",
    [
        get_score.description,
        get_course_schedule.description,
        SCORE_TOOL_DESCRIPTION,
        COURSE_TOOL_DESCRIPTION,
        SEMESTER_DESCRIPTION,
        WEEK_DESCRIPTION,
    ],
    ids=[
        "score-tool",
        "course-tool",
        "mcp-score",
        "mcp-course",
        "mcp-semester-arg",
        "mcp-week-arg",
    ],
)
def test_tool_descriptions_carry_no_calendar_facts(description: str) -> None:
    """直连与 MCP 的工具 / 参数描述都不再声明「当前为第 X 周 / 当前为 X 学期」。"""

    assert not _CURRENT_WEEK_CLAIM.search(description), (
        f"描述中出现「当前为第 N 教学周」的日历事实声明: {description!r}"
    )
    assert not _CURRENT_SEMESTER_CLAIM.search(description), (
        f"描述中出现「当前为 X 学期」的日历事实声明: {description!r}"
    )


def test_mcp_descriptions_still_explain_the_parameters() -> None:
    """去掉事实之后，参数说明本身仍然可用（类型语义与省略行为都在）。"""

    assert "教学周" in WEEK_DESCRIPTION
    assert "整学期课表" in WEEK_DESCRIPTION
    assert "学期" in SEMESTER_DESCRIPTION
    assert "全部成绩" in SEMESTER_DESCRIPTION


# ============================================================ 4. 守卫级：生产代码无第二份日历事实


def test_no_calendar_constant_assignments_in_production_code() -> None:
    """``app/`` 全目录不得再有 ``CURRENT_SEMESTER =`` / ``CURRENT_WEEK =`` 赋值。

    日历事实的唯一形态是配置层 dataclass 的 ``current_semester`` /
    ``current_week`` 字段（及带 ``_DEFAULT_`` 前缀的默认值常量），
    均不会命中本守卫的形状。
    """

    offenders = [
        str(path.relative_to(_APP_DIR))
        for path in sorted(_APP_DIR.rglob("*.py"))
        if _CALENDAR_CONSTANT_ASSIGNMENT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"生产代码里残留日历常量赋值: {offenders}"


def test_no_calendar_facts_claimed_as_current_in_production_code() -> None:
    """``app/`` 全目录不得出现「当前为第 X 教学周 / 当前为 X 学期」式声明。"""

    offenders = [
        str(path.relative_to(_APP_DIR))
        for path in sorted(_APP_DIR.rglob("*.py"))
        if _CURRENT_WEEK_CLAIM.search(text := path.read_text(encoding="utf-8"))
        or _CURRENT_SEMESTER_CLAIM.search(text)
    ]
    assert offenders == [], f"生产代码里残留「当前为 …」的日历事实声明: {offenders}"
