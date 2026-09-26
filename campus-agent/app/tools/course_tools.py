"""课程表查询工具（LangChain 侧）。

对外契约（勿随意变更）：
``get_course_schedule(week=None)`` 返回 ``list[dict[str, str]]``，每项含
``course`` / ``teacher`` / ``weekday`` / ``time`` / ``room`` / ``weeks`` 六个字段。
字段定义与归一化在 ``app.tools.course_contract`` 中单点维护，工具本身不含
任何课程数据字面量（由测试守卫强制）。

数据来源：校园业务服务（Spring Boot）的
``GET /api/courses?studentId=...&week=...``，由该服务经 Spring Data JPA
访问 MySQL。

因此本工具刻意保持「薄」——它只做四件事：

1. 校验 ``week`` 的取值（教学周范围）；
2. 调用 ``CampusApiClient`` 发起 HTTP 请求；
3. 把响应整理成 Agent 使用的那六个字符串字段；
4. 返回结果。

它**不**包含：SQL、数据库连接、URL 拼接、状态码判断、JSON 解析，
也不做「第几周有没有课」的判断——周次过滤由服务端按课程的周次区间
在数据库里完成（见 ``CourseService``）。把这条规则留在服务端，是因为它既需要
数据库的范围比较，也属于教务业务规则：在客户端再实现一遍，两份规则迟早会漂移。

失败一律由 ``CampusApiError`` 家族上抛，由统一的 ``handle_tool_error``
转成可读的 ``ToolMessage`` 交回模型，不中断整轮对话。

参数校验分两层（与改造前保持一致，工具签名未变）：

1. **schema 层**：``@tool`` 由函数签名生成 pydantic ``args_schema``，
   LLM 传错类型（例如 ``{"week": "abc"}``）在进入函数体前就被拒；
2. **取值层**：函数体内校验教学周范围，非法取值抛 ``ValueError``，
   由 ``handle_tool_error`` 转成错误消息回给模型（模型可据此在 ReAct
   循环里自行改参重试）。这一层顺手省掉一次注定失败的往返请求。
   范围与判定逻辑来自 ``app.tools.course_contract.validate_week``——
   与 MCP Server 上的同名工具共用同一份口径，不在这里另写一套。

注意：pydantic 默认按**宽松模式**转换，``"3"`` / ``True`` 这类取值会先被
强制转成整数，因此函数体里的类型分支只在**直接调用函数本体**时生效——
它是留给非 LLM 调用方的防线。
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.infrastructure.campus_api import get_campus_api_client
from app.tools.course_contract import (
    COURSE_FIELDS,
    MAX_WEEK,
    MIN_WEEK,
    normalize_course_rows,
    validate_week,
)

logger = logging.getLogger(__name__)

# ``week`` 的取值口径（``MIN_WEEK`` / ``MAX_WEEK`` / 校验实现）定义在
# ``app.tools.course_contract``——直连工具与 MCP Server 共用同一份，
# 这里只做再导出，保持既有 import 路径可用。
#
# 「当前是第几教学周」这一教务日历事实的唯一来源是配置层
# ``app.config.settings.get_campus_calendar_settings()``（工具描述不再携带具体值，
# 见该配置的 docstring）。
__all__ = ["COURSE_FIELDS", "MAX_WEEK", "MIN_WEEK", "get_course_schedule"]


@tool
def get_course_schedule(week: int | None = None) -> list[dict[str, str]]:
    """查询学生的课程安排（课表），返回课程名称、任课教师、星期、时间、教室与上课周次。

    数据来自校园教务系统的实时查询（不是本地固定数据）。
    返回结果按星期、上课时间升序；每项含六个字段：
    ``course``（课程名称）、``teacher``（任课教师）、``weekday``（星期）、
    ``time``（上课时段）、``room``（教室）、``weeks``（上课周次区间，如 1-16周）。

    当用户询问「我这周有什么课」「课表怎么安排」「某门课什么时候上」
    「在哪间教室上课」等与课程安排相关的问题时调用本工具。

    Args:
        week: 教学周序号，例如 3 表示第 3 教学周；省略时不限教学周，返回整学期课表。
    """

    target_week = validate_week(week)

    rows = get_campus_api_client().fetch_course_schedule(week=target_week)
    normalized = normalize_course_rows(rows)

    logger.debug("课表查询 week=%s 命中 %d 条", target_week, len(normalized))
    return normalized
