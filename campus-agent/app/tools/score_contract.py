"""成绩数据的对外契约：字段定义与归一化。

为什么单独成一个模块
--------------------

与 ``app.tools.exam_contract`` / ``app.tools.course_contract`` 同一套理由：
字段定义与归一化集中一处，工具本身才能保持「薄」——只做
「取参数 -> 调 ``CampusApiClient`` -> 归一化 -> 返回」，不含任何成绩数据字面量。

字段顺序即对外顺序，与 Spring Boot 的 ``ScoreResponse`` 一一对应::

    semester / course / score / credit

与其他两个契约的关键差异：**成绩与学分是数值，不是字符串**
--------------------------------------------------------

考试与课程的字段全是文本，因此那两份契约把所有值都 ``str()`` 化。成绩不同——
模型要直接比较大小（「哪门考得好」）与做加权（「绩点怎么算」），把 ``92.0``
变成 ``"92.0"`` 会逼它自己转换，而且字符串排序会退化成字典序。
因此这里对 ``NUMERIC_FIELDS`` 单独走数值归一化：
接受 JSON 数字（``int`` / ``float``）与数字字符串，统一输出 ``float``；
``bool`` 被显式拒绝（``bool`` 是 ``int`` 的子类，``true`` 不是成绩），
``NaN`` / ``Infinity`` 同样拒绝（非有限值无法参与任何有意义的大小比较）。

本模块**刻意不依赖 LangChain 与 MCP**：成绩能力现在同时经「直连工具」与
「MCP Server」暴露，两边都只引入本模块，不必把 LangChain 拖进 MCP Server 进程。

它同时承载两个查询参数的**唯一语义**（``normalize_optional_text`` 与
``match_course`` / ``select_by_course``）：直连工具与 MCP Server 上的 ``get_score``
必须表现完全一致，把这两段写两份迟早会漂移（同一个「网络」在一条链路上能匹配、
在另一条链路上匹配不到）。判定逻辑放在契约里，而不是任一方实现里，
就是为了让它只有一个家。

异常类型沿用 ``CampusServiceDataError``：数据格式不符是**上游业务服务**的问题，
归到既有的失败分类里，调用方（``handle_tool_error``）无需新增分支。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any, Final

from app.infrastructure.campus_api import CampusServiceDataError

#: 对外输出的字段；元组顺序即结果里的字段顺序。
SCORE_FIELDS: Final[tuple[str, ...]] = ("semester", "course", "score", "credit")

#: 必须保持**数值类型**的字段（其余字段按文本归一化）。
NUMERIC_FIELDS: Final[tuple[str, ...]] = ("score", "credit")

#: 查询串中常见的不可见字符：BOM 与零宽空格。
#: ``str.strip()`` 只按 ``isspace()`` 裁剪，而 ``'\ufeff'.isspace()`` 为 ``False``，
#: 带上会让「看似相同」的学期或课程名匹配失败，因此在入口统一剔除。
INVISIBLE_CHARS: Final[tuple[str, ...]] = ("\ufeff", "\u200b")


def normalize_optional_text(value: Any, *, name: str) -> str | None:
    """校验并归一化可选的文本型查询参数（直连工具与 MCP Server 的唯一口径）。

    非 ``str`` 且非 ``None`` 时抛 ``TypeError``；空串 / 纯空白视为「未提供」，
    返回 ``None`` 而不是让下游拿一个空字符串去做匹配
    （空串去做课程名包含匹配会命中全部记录，那是错的）。

    说明：经 ``ToolNode`` / MCP 入参校验到达这里时，类型已由 schema 把住，
    因此类型分支主要服务「直接调用函数本体」的场景。

    Raises:
        TypeError: 取值既不是 ``None`` 也不是字符串。
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(
            f"{name} 必须是字符串，收到 {type(value).__name__}: {value!r}"
        )
    for char in INVISIBLE_CHARS:
        value = value.replace(char, "")
    cleaned = value.strip()
    return cleaned or None


def match_course(row_course: str, query: str) -> bool:
    """课程名匹配：双向包含（``"某课课程"`` 与 ``"某课"`` 互相命中）。

    用双向包含是因为模型可能补全成「某课课程」，而用户口语又可能只给
    「某课」；只做单向包含会漏掉其中一种。
    """

    left, right = row_course.casefold(), query.casefold()
    return left in right or right in left


def select_by_course(
    rows: Iterable[dict[str, str | float]], course: str | None
) -> list[dict[str, str | float]]:
    """按课程名在**已取回的结果**里挑选（``course`` 为 ``None`` 时原样返回）。

    课程名筛选刻意留在 Python 侧而不是下推到 ``/api/scores``：接口契约只按学号
    与学期筛选，把接口扩成支持课程名模糊匹配，等于把一套匹配规则复制到服务端去。
    学期不同——它是数据库里的一列，且需要精确匹配，因此下推给服务端。
    """

    if course is None:
        return list(rows)
    return [row for row in rows if match_course(str(row.get("course", "")), course)]


def _as_number(value: Any, *, index: int, field_name: str) -> float:
    """把成绩 / 学分归一化成 ``float``，失败时抛出可读的格式异常。

    刻意不做范围校验（例如 0–100）：那是业务有效性，不是接口结构问题。
    分数越界说明数据库里的数据有问题，应该在数据侧暴露，而不是在这里被
    悄悄改写或拒绝——契约只负责「类型对不对、能不能用」。
    """

    if isinstance(value, bool):
        # bool 是 int 的子类，不拦住的话 True 会变成 1.0
        raise CampusServiceDataError(
            f"校园业务服务返回的成绩数据格式异常"
            f"（第 {index} 条的 {field_name} 是布尔值，不是数值），请联系管理员。"
        )

    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise CampusServiceDataError(
                f"校园业务服务返回的成绩数据格式异常"
                f"（第 {index} 条的 {field_name} 不是数值：{value!r}），请联系管理员。"
            ) from exc
    else:
        raise CampusServiceDataError(
            f"校园业务服务返回的成绩数据格式异常"
            f"（第 {index} 条的 {field_name} 类型为 {type(value).__name__}，不是数值），"
            f"请联系管理员。"
        )

    if not math.isfinite(number):
        raise CampusServiceDataError(
            f"校园业务服务返回的成绩数据格式异常"
            f"（第 {index} 条的 {field_name} 不是有限数值），请联系管理员。"
        )
    return number


def normalize_score_row(row: Mapping[str, Any], *, index: int) -> dict[str, str | float]:
    """把一条成绩记录整理成契约要求的四字段字典。

    业务服务侧已通过 DTO 收敛字段，这里再做一次显式归一化，理由是**跨语言边界
    不能靠约定**：Python 侧拿到的只是 JSON，对方字段缺失、改名为驼峰或返回
    ``null`` 时，如果直接透传下去，模型会拿到结构不一致的数据（时有时无的键），
    最终表现为回答里缺学期或缺学分。这里把它变成明确的失败，
    错误信息里带上第几条、缺哪个字段，便于定位。

    Args:
        row: 业务服务返回的单条记录。
        index: 该记录在结果中的序号（从 1 开始），仅用于错误定位。

    Returns:
        只含 ``SCORE_FIELDS`` 四个键的新字典：``semester`` / ``course`` 为
        去空白字符串，``score`` / ``credit`` 为 ``float``。

    Raises:
        CampusServiceDataError: 必填字段缺失、为空，或数值字段不是合法数值。
    """

    normalized: dict[str, str | float] = {}
    for field_name in SCORE_FIELDS:
        value = row.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CampusServiceDataError(
                f"校园业务服务返回的成绩数据格式异常"
                f"（第 {index} 条缺少字段 {field_name}），请联系管理员。"
            )
        if field_name in NUMERIC_FIELDS:
            normalized[field_name] = _as_number(value, index=index, field_name=field_name)
        else:
            normalized[field_name] = str(value).strip()
    return normalized


def normalize_score_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str | float]]:
    """逐条归一化成绩记录，序号从 1 开始（便于错误信息指向具体记录）。"""

    return [
        normalize_score_row(row, index=index) for index, row in enumerate(rows, start=1)
    ]


__all__ = [
    "INVISIBLE_CHARS",
    "NUMERIC_FIELDS",
    "SCORE_FIELDS",
    "match_course",
    "normalize_optional_text",
    "normalize_score_row",
    "normalize_score_rows",
    "select_by_course",
]
