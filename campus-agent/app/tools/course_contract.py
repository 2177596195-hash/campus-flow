"""课程数据的对外契约：字段定义与归一化。

为什么单独成一个模块
--------------------

与 ``app.tools.exam_contract`` 同一套理由，只是当前只有一个使用方（LangChain
工具 ``app.tools.course_tools``）：字段定义与归一化集中一处，工具本身才能保持
「薄」——只做「取参数 -> 调 ``CampusApiClient`` -> 归一化 -> 返回」，不含任何
课程数据字面量。

字段顺序即对外顺序，与 Spring Boot 的 ``CourseScheduleResponse`` 一一对应::

    course / teacher / weekday / time / room / weeks

其中 ``weekday``（``周一``…``周日``）与 ``weeks``（``1-16周``）在服务端就是
**渲染好**的字符串：库里存的是星期序号与周次区间，目的是让「按周查询」能走
数据库的范围比较。Python 侧因此不需要（也不应该）重新推导这两个字段——
多一处推导就多一处可能和服务端不一致的实现。

本模块**刻意不依赖 LangChain 与 MCP**：课程能力现在同时经「直连工具」与
「MCP Server」暴露，两边都只引入本模块，不必把 LangChain 拖进 MCP Server 进程。

它同时承载 ``week`` 参数的**唯一取值口径**（``MIN_WEEK`` / ``MAX_WEEK`` /
``validate_week``）：直连工具与 MCP Server 的 ``week`` 语义必须完全一致，
把边界写两份迟早会漂移（一边改了另一边没改，同一个「第 30 周」在两条链路上
一个被接受、一个被拒）。校验逻辑放在契约里，而不是任一方实现里，
就是为了让它只有一个家。

异常类型沿用 ``CampusServiceDataError``：数据格式不符是**上游业务服务**的问题，
归到既有的失败分类里，调用方（``handle_tool_error``）无需新增分支。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from app.infrastructure.campus_api import CampusServiceDataError

#: 教学周允许范围。直连工具与 MCP Server 共用，避免两边各写一份。
MIN_WEEK: Final[int] = 1
MAX_WEEK: Final[int] = 30

#: 对外输出的字段；元组顺序即结果里的字段顺序。
COURSE_FIELDS: Final[tuple[str, ...]] = (
    "course",
    "teacher",
    "weekday",
    "time",
    "room",
    "weeks",
)


def validate_week(week: Any) -> int | None:
    """校验并归一化 ``week`` 参数（直连工具与 MCP Server 的唯一口径）。

    ``None`` 表示「不限教学周」，直接放行；其余取值必须是整数且落在
    ``[MIN_WEEK, MAX_WEEK]`` 内。``bool`` 是 ``int`` 的子类，会被显式拒绝：
    ``True`` 当周次传进来几乎总是调用方的错误，静默当成第 1 周会掩盖问题。

    说明：经 ``ToolNode`` / MCP 入参校验到达这里时，``"3"`` 这类字符串已被
    上游按类型声明转换成整数，所以类型分支主要服务「直接调用函数本体」的场景。

    Raises:
        TypeError: 取值既不是 ``None`` 也不是整数（含 ``bool``）。
        ValueError: 取值超出教学周范围。
    """

    if week is None:
        return None
    if isinstance(week, bool) or not isinstance(week, int):
        raise TypeError(
            f"week 必须是整数（教学周序号），收到 {type(week).__name__}: {week!r}"
        )
    if not MIN_WEEK <= week <= MAX_WEEK:
        raise ValueError(f"week 必须落在 {MIN_WEEK}-{MAX_WEEK} 之间，收到 {week}")
    return week


def normalize_course_row(row: Mapping[str, Any], *, index: int) -> dict[str, str]:
    """把一条课程记录整理成契约要求的六字段字符串字典。

    业务服务侧已通过 DTO 收敛字段，这里再做一次显式归一化，理由是**跨语言边界
    不能靠约定**：Python 侧拿到的只是 JSON，对方字段缺失、改名为驼峰或返回
    ``null`` 时，如果直接透传下去，模型会拿到结构不一致的数据（时有时无的键），
    最终表现为回答里缺教室或缺周次。这里把它变成明确的失败，
    错误信息里带上第几条、缺哪个字段，便于定位。

    Args:
        row: 业务服务返回的单条记录。
        index: 该记录在结果中的序号（从 1 开始），仅用于错误定位。

    Returns:
        只含 ``COURSE_FIELDS`` 六个键、值均为去空白字符串的新字典。

    Raises:
        CampusServiceDataError: 必填字段缺失或为空。
    """

    normalized: dict[str, str] = {}
    for field_name in COURSE_FIELDS:
        value = row.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CampusServiceDataError(
                f"校园业务服务返回的课程数据格式异常"
                f"（第 {index} 条缺少字段 {field_name}），请联系管理员。"
            )
        normalized[field_name] = str(value).strip()
    return normalized


def normalize_course_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """逐条归一化课程记录，序号从 1 开始（便于错误信息指向具体记录）。"""

    return [
        normalize_course_row(row, index=index) for index, row in enumerate(rows, start=1)
    ]


__all__ = [
    "COURSE_FIELDS",
    "MAX_WEEK",
    "MIN_WEEK",
    "normalize_course_row",
    "normalize_course_rows",
    "validate_week",
]
