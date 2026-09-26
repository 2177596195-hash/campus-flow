"""请假申请的对外契约：结果字段定义与参数 / 结果归一化。

为什么单独成一个模块
--------------------

与 ``app.tools.exam_contract`` / ``course_contract`` / ``score_contract`` 同一套理由：
字段定义与归一化集中一处，「提交请假申请」这条链路才能保持薄——只做
「校验参数 -> 调 ``CampusApiClient`` -> 归一化 -> 返回」，不含任何请假数据字面量。

本模块**刻意不依赖 LangChain 与 MCP**：请假能力现在只经 MCP Server 暴露
（写操作不进 ``AGENT_TOOLS``），因此 MCP Server 进程里不需要为了这份契约
把 LangChain 拖进来。

与另外三份契约的关键差异：**这里是写操作，字段语义是「提交」不是「查询」**
------------------------------------------------------------------------

考试 / 课程 / 成绩三份契约都在描述「返回哪些字段」，本模块还要承担
**入参**的语义——因为参数由模型给出，而模型可能给出人能看懂、但机器不接受
的写法（``9月28日``、``2026/09/28 8:0``）。因此这里提供三个入参归一化函数，
把「模型怎么写」收敛成「服务端唯一接受的形态」：

- :func:`normalize_required_text`：空串 / 纯空白视为**缺失**并直接失败，
  而不是把一个空字符串提交上去（空事由是一条无意义的申请）；
- :func:`normalize_datetime`：接受 ISO 8601 的常见写法（``2026-10-12 08:00``、
  ``2026-10-12T08:00``、``2026-10-12T08:00:00``，以及只有日期的 ``2026-10-12``），
  统一输出 ``2026-10-12T08:00``。失败时抛出**可读中文的 ``ValueError``**——
  它会被 ``handle_tool_error`` 原样写进 ``ToolMessage``，模型据此改参重试
  （``Tool Calling`` 阶段已有「模型据错误消息自行改参重试」的用例）。

入参失败用 ``TypeError`` / ``ValueError`` 而不是 ``CampusServiceDataError``：
后者表达的是「**上游服务**返回的数据不合约定」，而入参不合约定是调用方的问题，
两者混在一起会让模型以为服务坏了、从而放弃重试。
异常类型沿用既有习惯（``app.tools.course_contract.validate_week`` 同样抛
``ValueError``），因此 ``handle_tool_error`` 侧无需新增分支。

结果字段顺序即对外顺序，与 Spring Boot 的 ``LeaveApplicationResponse`` 一一对应::

    id / studentId / startTime / endTime / reason / status / createdAt

字段全部按文本归一化（含 ``id``）：它们是给用户复述与记录的标识，
不参与任何计算，字符串化不会带来损失，却让「模型拿到的东西」结构稳定。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 对外输出的结果字段；元组顺序即结果里的字段顺序。
#: 与 Spring Boot ``LeaveApplicationResponse`` 的 record 组件一一对应（7 个）。
LEAVE_RESULT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "studentId",
    "startTime",
    "endTime",
    "reason",
    "status",
    "createdAt",
)

#: 提交成功后的初始状态（与 Spring Boot 侧 ``LeaveApplication.STATUS_PENDING``
#: 同值）。写成常量是为了让 MCP 工具的描述、用例断言与 Java 侧不各写一份字面量。
STATUS_PENDING: Final[str] = "PENDING"

#: 「待审核」的自然语言说法。工具描述与用例共用，避免同一句话有两个版本。
STATUS_PENDING_LABEL: Final[str] = "待审核"

#: 时间入参的期望写法（会出现在工具描述与错误消息里，只此一处）。
#: 示例刻意不取 MySQL 种子数据里已有的考试日期（与三份查询契约「举例不用真实数据」
#: 的做法一致）：描述文本里出现真实业务日期会削弱「数据只在 MySQL」这条守卫。
DATETIME_HINT: Final[str] = "形如 2026-10-12 08:00"


def normalize_required_text(value: Any, *, name: str) -> str:
    """校验并归一化必填的文本入参；空串 / 纯空白视为缺失。

    Raises:
        TypeError: 取值既不是 ``None`` 也不是字符串。
        ValueError: 取值为空或纯空白。
    """

    if value is None:
        raise ValueError(f"{name}不能为空，请补充后再提交请假申请。")
    if not isinstance(value, str):
        raise TypeError(f"{name}必须是字符串，收到 {type(value).__name__}: {value!r}")

    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name}不能为空，请补充后再提交请假申请。")
    return cleaned


def normalize_datetime(value: Any, *, name: str) -> str:
    """把时间入参归一成 ``2026-10-12T08:00`` 形态的 ISO 8601 文本。

    接受 ``datetime.fromisoformat`` 认得的常见写法（含只给日期的 ``2026-10-12``，
    按当天零点处理），统一输出到**分钟**精度——秒以下的精度对请假没有意义，
    却会让同一份请求在不同模型手里变出不同字符串。

    Raises:
        TypeError: 取值既不是 ``None`` 也不是字符串。
        ValueError: 取值无法解析成时间。
    """

    if value is None:
        raise ValueError(f"{name}不能为空，请给出{DATETIME_HINT}。")
    if not isinstance(value, str):
        raise TypeError(f"{name}必须是字符串，收到 {type(value).__name__}: {value!r}")

    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name}不能为空，请给出{DATETIME_HINT}。")

    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"{name}格式无法识别：{cleaned!r}，请使用{DATETIME_HINT}的格式。"
        ) from exc
    return parsed.isoformat(timespec="minutes")


def normalize_leave_result(row: Mapping[str, Any]) -> dict[str, str]:
    """把服务端返回的申请记录整理成契约要求的七个字段。

    与成绩契约同一套理由：跨语言边界不能靠约定。Python 侧拿到的只是 JSON，
    对方字段缺失、改名为驼峰或返回 ``null`` 时若直接透传，模型会拿到结构不一致的
    数据（时有时无的键），最终表现为回答里缺申请编号或缺状态。这里把它变成明确的
    失败，错误信息里带上缺哪个字段，便于定位。

    Args:
        row: 业务服务返回的单条记录。

    Returns:
        只含 :data:`LEAVE_RESULT_FIELDS` 七个键的新字典，取值均为去空白字符串。

    Raises:
        CampusServiceDataError: 必填字段缺失或为空。
    """

    # 延迟导入：本模块不依赖 LangChain / MCP，也不该在导入期拉起基础设施包。
    from app.infrastructure.campus_api import CampusServiceDataError

    if not isinstance(row, Mapping):
        raise CampusServiceDataError(
            "校园业务服务返回的请假申请数据格式异常（不是对象），请联系管理员。"
        )

    normalized: dict[str, str] = {}
    for field_name in LEAVE_RESULT_FIELDS:
        value = row.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CampusServiceDataError(
                f"校园业务服务返回的请假申请数据格式异常"
                f"（缺少字段 {field_name}），请联系管理员。"
            )
        normalized[field_name] = str(value).strip()
    return normalized


__all__ = [
    "DATETIME_HINT",
    "LEAVE_RESULT_FIELDS",
    "STATUS_PENDING",
    "STATUS_PENDING_LABEL",
    "normalize_datetime",
    "normalize_leave_result",
    "normalize_required_text",
]
