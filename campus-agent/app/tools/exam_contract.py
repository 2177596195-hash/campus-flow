"""考试数据的对外契约：字段定义与归一化。

为什么单独成一个模块
--------------------

「考试安排」现在有两个使用方：LangChain 工具（``app.tools.exam_tools``）与
MCP 工具（``app.mcp.server``）。它们必须吐出**完全同一组字段**，否则模型
会在两条链路上看到结构不一致的数据。

把定义放在本模块而不是各自写一遍，是为了消除最典型的漂移形态——
「两处各写一遍四字段，改了一处、忘了另一处」：那种情况下不会报错，
只是某一方静默地少一个 key，最终表现为模型回答里缺考场或缺时间。

本模块**刻意不依赖 LangChain 与 MCP**：

- 不依赖 LangChain，MCP Server 才能只引入本模块，不必把 LangChain 拖进
  自己的进程（MCP Server 应当只依赖 ``CampusApiClient``）；
- 不依赖 MCP，LangChain 工具侧才能继续在无 MCP 环境下工作。

异常类型沿用 ``CampusServiceDataError``：数据格式不符是**上游业务服务**
的问题，归到既有的失败分类里，调用方（``handle_tool_error``）无需新增分支。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from app.infrastructure.campus_api import CampusServiceDataError

#: 对外输出的字段；元组顺序即结果里的字段顺序。
#: 两个使用方（LangChain 工具 / MCP 工具）共用这一份定义。
EXAM_FIELDS: Final[tuple[str, ...]] = ("course", "date", "time", "room")


def normalize_exam_row(row: Mapping[str, Any], *, index: int) -> dict[str, str]:
    """把一条考试记录整理成契约要求的四字段字符串字典。

    业务服务侧已通过 DTO 收敛字段，这里再做一次显式归一化，理由是**跨语言边界
    不能靠约定**：Python 侧拿到的只是 JSON，对方字段缺失、改名为驼峰、
    或返回 ``null`` 时，如果直接透传下去，模型会拿到结构不一致的数据
    （时有时无的键），最终影响自然语言回答的质量。这里把它变成明确的失败，
    错误信息里带上第几条、缺哪个字段，便于定位。

    Args:
        row: 业务服务返回的单条记录。
        index: 该记录在结果中的序号（从 1 开始），仅用于错误定位。

    Returns:
        只含 ``EXAM_FIELDS`` 四个键、值均为去空白字符串的新字典。

    Raises:
        CampusServiceDataError: 必填字段缺失或为空。
    """

    normalized: dict[str, str] = {}
    for field_name in EXAM_FIELDS:
        value = row.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CampusServiceDataError(
                f"校园业务服务返回的考试数据格式异常"
                f"（第 {index} 条缺少字段 {field_name}），请联系管理员。"
            )
        normalized[field_name] = str(value).strip()
    return normalized


def normalize_exam_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """逐条归一化考试记录，序号从 1 开始（便于错误信息指向具体记录）。"""

    return [
        normalize_exam_row(row, index=index) for index, row in enumerate(rows, start=1)
    ]


__all__ = ["EXAM_FIELDS", "normalize_exam_row", "normalize_exam_rows"]
