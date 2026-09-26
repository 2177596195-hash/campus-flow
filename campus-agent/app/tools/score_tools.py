"""成绩查询工具（LangChain 侧）。

对外契约（勿随意变更）：
``get_score(semester=None, course=None)`` 返回 ``list[dict[str, str | float]]``，
每项含 ``semester`` / ``course`` / ``score`` / ``credit`` 四个字段，其中
``score`` 与 ``credit`` 保持**数值类型**（便于模型直接比较与加权）。
字段定义、归一化与两个查询参数的语义都在 ``app.tools.score_contract`` 中单点维护，
工具本身不含任何成绩数据字面量（由测试守卫强制）。

数据来源：校园业务服务（Spring Boot）的
``GET /api/scores?studentId=...&semester=...``，由该服务经 Spring Data JPA
访问 MySQL。

因此本工具刻意保持「薄」——它只做四件事：

1. 校验并规整参数；
2. 调用 ``CampusApiClient`` 发起 HTTP 请求；
3. 把响应整理成 Agent 使用的那四个字段；
4. 返回结果。

它**不**包含：SQL、数据库连接、URL 拼接、状态码判断、JSON 解析。

两个参数的处理方式不同，这是刻意的
----------------------------------

- ``semester``：**查询下推**。过滤在服务端完成（Spring Boot 的 ``/api/scores``
  按 ``semester = ?`` 精确匹配），本工具只把条件透传成 query 参数。
  学期是数据库里的一列，「哪些学期有成绩」也属于业务事实，在客户端再判断一次
  就是第二份事实来源。
- ``course``：**本地挑选**。接口契约只按学号与学期筛选，「某门课的成绩」是在
  已取回的那一小段列表里再做一次课程名匹配（双向包含，容忍模型把「某课」
  补全成「某课课程」）。这与课程表工具里「按周过滤属于服务端」并不矛盾：
  周次过滤需要数据库做范围比较并走索引，课程名匹配只是一次针对几条记录的挑选；
  把接口扩成支持课程名模糊匹配，反而要把一套匹配规则复制到服务端去。

失败一律由 ``CampusApiError`` 家族上抛，由统一的 ``handle_tool_error``
转成可读的 ``ToolMessage`` 交回模型，不中断整轮对话。

参数校验分两层（与改造前保持一致，工具签名未变）：

1. **schema 层**：``@tool`` 由函数签名生成 pydantic ``args_schema``，
   LLM 传错类型在进入函数体前就被拒；
2. **取值层**：函数体内规整文本参数（剔除不可见字符、空串视为未提供），
   类型不符抛 ``TypeError``，由 ``handle_tool_error`` 转成错误消息回给模型。

这两层的实现都来自 ``app.tools.score_contract``（``normalize_optional_text`` /
``select_by_course``）——MCP Server 上的同名工具用的是**同一对函数**，
因此「同一个词在两条链路上匹配结果不同」这类漂移不会发生。

注意：pydantic 默认按**宽松模式**转换，因此函数体里的类型分支只在
**直接调用函数本体**时生效——它是留给非 LLM 调用方的防线。
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.infrastructure.campus_api import get_campus_api_client
from app.tools.score_contract import (
    SCORE_FIELDS,
    normalize_optional_text,
    normalize_score_rows,
    select_by_course,
)

logger = logging.getLogger(__name__)

__all__ = ["SCORE_FIELDS", "get_score"]


@tool
def get_score(semester: str | None = None, course: str | None = None) -> list[dict[str, str | float]]:
    """查询学生的考试成绩，返回学期、课程名称、成绩分数与学分。

    数据来自校园教务系统的实时查询（不是本地固定数据）。
    每项含四个字段：``semester``（学期，形如 2025-2026-1）、``course``（课程名称）、
    ``score``（成绩分数，数值）、``credit``（学分，数值）；分数与学分是数字，
    可直接比较大小与做加权计算。

    当用户询问「某门课考了多少分」「这学期成绩怎么样」「上学期成绩有哪些」
    等与考试成绩相关的问题时调用本工具。

    Args:
        semester: 学期，形如 ``2025-2026-1``；省略时不限学期，返回全部成绩。
        course: 课程名称，例如「微观经济学」；省略时返回该范围内全部课程。
            支持模糊匹配（课程名双向包含）。
    """

    target_semester = normalize_optional_text(semester, name="semester")
    target_course = normalize_optional_text(course, name="course")

    # 学期过滤在服务端完成；course 传空，不在请求里出现。
    rows = get_campus_api_client().fetch_scores(semester=target_semester)
    selected = select_by_course(normalize_score_rows(rows), target_course)

    logger.debug(
        "成绩查询 semester=%s course=%s 命中 %d 条",
        target_semester,
        target_course,
        len(selected),
    )
    return selected
