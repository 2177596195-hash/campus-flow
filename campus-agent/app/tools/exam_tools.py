"""考试安排查询工具（LangChain 侧）。

对外契约（勿随意变更）：
``get_exam_schedule()`` 返回 ``list[dict[str, str]]``，每项含
``course`` / ``date`` / ``time`` / ``room`` 四个字段。
字段定义与归一化在 ``app.tools.exam_contract`` 中单点维护，
MCP 侧的工具复用同一份定义。

数据来源：校园业务服务（Spring Boot）的 ``GET /api/exams?studentId=...``，
由该服务经 Spring Data JPA 访问 MySQL。

因此本工具刻意保持「薄」——它只做四件事：

1. 取得当前查询参数（当前学生标识来自配置）；
2. 调用 ``CampusApiClient`` 发起 HTTP 请求；
3. 把响应整理成 Agent 使用的那四个字符串字段；
4. 返回结果。

它**不**包含：SQL、数据库连接、URL 拼接、状态码判断、JSON 解析，
也不含任何本地固定考试数据（由测试守卫强制）。
失败一律由 ``CampusApiError`` 家族上抛，由统一的 ``handle_tool_error``
转成可读的 ``ToolMessage`` 交回模型，不中断整轮对话。

与 MCP 的关系
-------------

本工具是「直连」路径：Agent -> 工具 -> ``CampusApiClient`` -> Spring Boot。
当 ``CAMPUS_MCP_ENABLED=true`` 时，``app.mcp.tools.build_agent_tools()`` 会
**用 MCP 工具替换本工具在图中的槽位**（而不是两个同时注册），此时链路上会多出
Campus MCP Server 一跳。两条路径都只经由 ``CampusApiClient`` 访问业务服务，
不存在第二套 HTTP 逻辑。本模块始终保留，供 MCP 关闭时使用，也是 MCP 侧
行为的对照基线。
"""

from __future__ import annotations

from langchain_core.tools import tool

from app.infrastructure.campus_api import get_campus_api_client
from app.tools.exam_contract import EXAM_FIELDS, normalize_exam_rows

__all__ = ["EXAM_FIELDS", "get_exam_schedule"]


@tool
def get_exam_schedule() -> list[dict[str, str]]:
    """查询学生近期的考试安排，返回考试科目、日期、时间与考场。

    数据来自校园教务系统的实时查询（不是本地固定数据）。
    返回该学生的**全部**考试安排，按考试时间升序；每项含四个字段：
    ``course``（科目）、``date``（日期 yyyy-MM-dd）、
    ``time``（开考时间 HH:mm）、``room``（考场）。

    当用户询问「我最近有什么考试」「什么时候考某门课」「在哪个考场考」
    等与考试安排相关的问题时调用本工具。若用户只关心其中某一门课，
    请从返回结果中筛选出对应科目再作答。
    """

    rows = get_campus_api_client().fetch_exam_schedule()
    return normalize_exam_rows(rows)
