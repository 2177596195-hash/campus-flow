"""Campus MCP Server（stdio transport）。

职责（只有这三件事）
--------------------

1. 暴露 MCP 工具（当前四个：``get_exam_schedule`` / ``get_course_schedule`` /
   ``get_score`` 三个**只读查询**，以及 ``submit_leave_application`` 一个
   **写操作**；它们的入参都含 ``student_id``，课程工具另带可选 ``week``，
   成绩工具另带可选 ``semester`` 与 ``course``，请假工具另带必填的
   ``start_time`` / ``end_time`` / ``reason``）；
2. 调用**既有的** ``CampusApiClient`` 取数 / 提交（不新建第二套访问方式）；
3. 把结果整理成 MCP Tool Result——
   考试 ``course`` / ``date`` / ``time`` / ``room``，
   课程 ``course`` / ``teacher`` / ``weekday`` / ``time`` / ``room`` / ``weeks``，
   成绩 ``semester`` / ``course`` / ``score`` / ``credit``，
   请假申请 ``id`` / ``studentId`` / ``startTime`` / ``endTime`` / ``reason`` /
   ``status`` / ``createdAt``。

它**不**连接 MySQL、**不**写 SQL、**不**拼 URL、**不**处理 HTTP 状态码——
那些都在 Spring Boot 与 ``app.infrastructure.campus_api`` 里，本文件不复制一份。

四个工具**共用一个 Server 进程、一条 stdio 会话**：MCP 的价值之一是会话级的
能力协商与工具发现（``initialize`` + ``tools/list`` 只做一次）。为每项能力各起
一个 Server，会让 Agent 侧多出多份连接与多套生命周期，也让工具发现做多次。

参数的过滤位置在三个查询工具上刻意不同，且与服务端接口能力对齐
------------------------------------------------------------

- 考试：无过滤参数；
- 课程：``week`` **在服务端**过滤（``week_start <= week <= week_end``，
  需要数据库做范围比较并走索引）；
- 成绩：``semester`` **在服务端**过滤（``semester = ?`` 精确匹配），
  ``course`` **在本进程内**过滤（成绩接口不提供课程参数，
  课程名匹配只是取回后的挑选，不值得把一套模糊匹配规则复制进 Java 侧）。

因此本文件里能看到的过滤只有「成绩的 course」一处，且完全复用
``app.tools.score_contract.select_by_course``——与直连工具是同一个函数。

写工具（请假提交）的三条额外纪律
--------------------------------

1. **描述必须说清是「提交」**。模型最容易犯的错是把「我请假了没」之类的**询问**
   也当成提交意图，凭空造一条申请出来。因此描述里显式写明「提交 / 不查询 / 不审批」，
   并给出触发语（「我要请假」「帮我提交请假申请」）。
2. **参数在进入 HTTP 之前归一化**（``app.tools.leave_contract``）：时间统一成
   ISO 8601、空串视为缺失。归一化放在契约里而不是本文件里，是为了让「模型怎么写」
   只有一处解释；失败一律走 ``ToolError``，让模型看到原因并改参重试，而不是
   把一条半成品请求发给业务服务。
3. **不做任何审批或状态变更**：``status`` 由数据库默认值给（``PENDING``），
   Server 不传、不改。审批（通过 / 驳回）属于后续阶段，不在本阶段能力范围内。

启动方式::

    python -m app.mcp.server        # stdio transport，由 MCP Client 拉起

**stdout 是协议通道**：任何 ``print`` 或写向 stdout 的日志都会破坏 MCP 报文，
因此日志显式写 stderr（``_configure_logging``）。

也可以不经进程直接测试：``build_server().call_tool(...)``（SDK 内置的进程内调用），
``tests/mcp/`` 用它做不依赖子进程的用例。
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any, Final

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.infrastructure.campus_api import CampusApiError, get_campus_api_client
from app.tools.course_contract import normalize_course_rows, validate_week
from app.tools.exam_contract import normalize_exam_rows
from app.tools.leave_contract import (
    DATETIME_HINT,
    STATUS_PENDING_LABEL,
    normalize_datetime,
    normalize_leave_result,
    normalize_required_text,
)
from app.tools.score_contract import (
    normalize_optional_text,
    normalize_score_rows,
    select_by_course,
)

logger = logging.getLogger(__name__)

#: MCP Server 名称（客户端 ``initialize`` 时可见）
SERVER_NAME: Final[str] = "campus-mcp-server"

#: 注册的工具名。四个工具跑在**同一个** Server 里（见模块 docstring）。
TOOL_GET_EXAM_SCHEDULE: Final[str] = "get_exam_schedule"
TOOL_GET_COURSE_SCHEDULE: Final[str] = "get_course_schedule"
TOOL_GET_SCORE: Final[str] = "get_score"
#: 写工具：提交请假申请（唯一会改变数据库状态的能力）
TOOL_SUBMIT_LEAVE_APPLICATION: Final[str] = "submit_leave_application"

#: 考试工具描述。这段文字会随 ``tools/list`` 交给模型，因此按「给模型看的说明书」写：
#: 说清数据实时性（降低编造概率）、说清返回字段（模型据此组织回答）、
#: 说清何时该调用（与 MCP 关闭时直连工具的描述保持同等的信息量，
#: 否则「走 MCP」会连带改变模型的选择行为）。
EXAM_TOOL_DESCRIPTION: Final[str] = (
    "查询指定学生的近期考试安排。"
    "数据来自校园教务系统的实时查询（不是本地固定数据）。"
    "返回该学生的全部考试安排，按考试时间升序；每项含四个字段："
    "course（考试科目）、date（考试日期 yyyy-MM-dd）、"
    "time（开考时间 HH:mm）、room（考场）。"
    "当用户询问「我最近有什么考试」「什么时候考某门课」「在哪个考场考」"
    "等与考试安排相关的问题时调用本工具。"
    "若用户只关心其中某一门课，请从返回结果中筛选出对应科目再作答。"
)

#: 课程工具描述。与直连工具的描述保持同等信息量（同样的字段、同样的实时性声明），
#: 否则「改走 MCP」会连带改变模型的选择行为。
#: 教务日历事实（当前学期 / 当前教学周）**刻意不写进任何工具描述**：
#: 描述是静态说明书，不该成为事实数据的第二份来源——换学期时改配置
#: （``CAMPUS_CURRENT_SEMESTER`` / ``CAMPUS_CURRENT_WEEK``）即可，不用改代码。
COURSE_TOOL_DESCRIPTION: Final[str] = (
    "查询学生的课程安排（课表），返回课程名称、任课教师、星期、时间、教室与上课周次。"
    "数据来自校园教务系统的实时查询（不是本地固定数据）。"
    "返回结果按星期、上课时间升序；每项含六个字段："
    "course（课程名称）、teacher（任课教师）、weekday（星期）、"
    "time（上课时段）、room（教室）、weeks（上课周次区间，如 1-16周）。"
    "当用户询问「我这周有什么课」「课表怎么安排」「某门课什么时候上」"
    "「在哪间教室上课」等与课程安排相关的问题时调用本工具。"
)

#: 成绩工具描述。与直连工具的描述保持同等信息量（同样的四字段、同样的实时性声明、
#: 同样点明「分数与学分是数字」），否则「改走 MCP」会连带改变模型的选择与作答行为。
SCORE_TOOL_DESCRIPTION: Final[str] = (
    "查询学生的考试成绩，返回学期、课程名称、成绩分数与学分。"
    "数据来自校园教务系统的实时查询（不是本地固定数据）。"
    "每项含四个字段：semester（学期，形如 2025-2026-1）、course（课程名称）、"
    "score（成绩分数，数值）、credit（学分，数值）；分数与学分是数字，"
    "可直接比较大小与做加权计算。"
    "当用户询问「某门课考了多少分」「这学期成绩怎么样」「上学期成绩有哪些」"
    "等与考试成绩相关的问题时调用本工具。"
)

#: ``student_id`` 参数的说明（会进入 MCP Tool 的 inputSchema）
STUDENT_ID_DESCRIPTION: Final[str] = "学生学号，例如 student-001"

#: ``week`` 参数的说明（同样会进入 inputSchema，是模型判断「要不要传这个参数」的依据）。
#: 具体周次值由配置层统一维护（见 COURSE_TOOL_DESCRIPTION 的说明），不在此复制。
WEEK_DESCRIPTION: Final[str] = (
    "教学周序号，例如 3 表示第 3 教学周；省略时不限教学周，返回整学期课表。"
)

#: ``semester`` 参数的说明。当前学期由配置层统一维护，不在此复制具体值。
SEMESTER_DESCRIPTION: Final[str] = (
    "学期，形如 2025-2026-1；省略时不限学期，返回全部成绩。"
)

#: ``course`` 参数的说明。写清「支持模糊匹配」是因为匹配发生在 Server 进程内
#: （``select_by_course`` 双向包含），模型不必把课名补全得一字不差。
#: 举例刻意选一门**不在种子数据里**的课（与直连工具 docstring 的举例一致）：
#: 描述文本里出现真实成绩数据会削弱「数据只在 MySQL」这条守卫。
COURSE_NAME_DESCRIPTION: Final[str] = (
    "课程名称，例如「微观经济学」；省略时返回该范围内全部课程。"
    "支持模糊匹配（课程名双向包含）。"
)

#: 请假提交工具描述。**这是本 Server 上唯一的写操作**，因此这段文字的首要任务是
#: 把「提交」与「查询」划清界限：模型若把「我的请假批了吗」当成提交，就会凭空
#: 造出一条申请（写操作无法像查询那样「答错也没关系」）。
#: 三件事必须写清楚——什么时候调用、三个参数各自是什么、提交成功后返回什么。
LEAVE_TOOL_DESCRIPTION: Final[str] = (
    "提交学生的请假申请（写操作：会真实创建一条请假记录并进入待审核状态）。"
    "当用户表达「我要请假」「帮我请个假」「提交请假申请」这类**提交**意图时调用本工具；"
    "请先确认请假的开始时间、结束时间与事由都已明确，缺哪项就先向用户询问，"
    "不要自行编造时间或事由。"
    "本工具只负责**提交**：不能查询已有请假记录、不能审批（通过 / 驳回）、"
    "不能撤销或修改。用户只是询问请假制度、流程或结果时不要调用本工具。"
    "提交成功后返回申请编号 id、当前状态 status"
    f"（{STATUS_PENDING_LABEL}）、以及起止时间与事由，"
    "请把申请编号与状态告知用户。"
)

#: 三个请假参数的说明。格式说明只写一次（``DATETIME_HINT``），避免描述与
#: 归一化实现各说一套；「示例」刻意给出与提示一致的写法，模型照抄即可通过校验。
START_TIME_DESCRIPTION: Final[str] = (
    f"请假开始时间，{DATETIME_HINT}，例如 2026-10-12 08:00。"
    "必须是明确的日期与时刻，不要只说「明天」这类相对说法。"
)

END_TIME_DESCRIPTION: Final[str] = (
    f"请假结束时间，{DATETIME_HINT}，例如 2026-10-14 18:00。"
    "必须晚于或等于开始时间。"
)

REASON_DESCRIPTION: Final[str] = (
    "请假事由，例如「感冒发烧需就医」。必填，请用用户原话概括，不要留空、不要编造。"
)


def build_server() -> MCPServer:
    """构建 Campus MCP Server 并注册全部工具。

    当前：三个只读查询（考试安排、课程表、考试成绩）+ 一个写操作（提交请假申请）。

    返回未启动的 Server 实例：调用方决定用哪种 transport
    （``run(transport="stdio")`` 或进程内直接 ``call_tool``）。
    因此本函数可在测试里安全调用，不产生子进程。
    """

    server = MCPServer(
        SERVER_NAME,
        description=(
            "CampusFlow 校园业务 MCP Server"
            "（提供考试安排、课程表、成绩查询与请假申请提交）"
        ),
    )

    @server.tool(name=TOOL_GET_EXAM_SCHEDULE, description=EXAM_TOOL_DESCRIPTION)
    def get_exam_schedule(
        student_id: Annotated[str, Field(description=STUDENT_ID_DESCRIPTION)],
    ) -> list[dict[str, str]]:
        """查询指定学生的近期考试安排。

        Args:
            student_id: 学生学号，例如 ``student-001``。

        Returns:
            考试记录列表，每项含 ``course`` / ``date`` / ``time`` / ``room``。

        Raises:
            ToolError: 业务服务不可用、拒绝请求、内部错误或返回结构异常。
                **必须**用 ``ToolError`` 而不是任意异常——按 MCP SDK 的约定，
                只有 ``ToolError`` 会把原始消息交给模型；其它异常一律被替换成
                ``Error executing tool <name>``，业务原因就到不了 Agent，
                用户只会看到一句无意义的英文。
        """

        logger.info("MCP 工具调用: %s student_id=%s", TOOL_GET_EXAM_SCHEDULE, student_id)
        try:
            rows: list[dict[str, Any]] = get_campus_api_client().fetch_exam_schedule(
                student_id
            )
            normalized = normalize_exam_rows(rows)
        except CampusApiError as exc:
            # 可预期的业务失败（服务不可达 / 4xx / 5xx / 数据格式异常）：
            # CampusApiError 家族的 str() 已经是可直接转述给用户的中文，
            # 这里原样交给工具错误通道，让模型能据此向用户解释。
            logger.warning("MCP 工具业务失败: %s: %s", type(exc).__name__, exc)
            raise ToolError(str(exc)) from exc
        logger.info("MCP 工具返回 %d 条考试记录", len(normalized))
        return normalized

    @server.tool(name=TOOL_GET_COURSE_SCHEDULE, description=COURSE_TOOL_DESCRIPTION)
    def get_course_schedule(
        student_id: Annotated[str, Field(description=STUDENT_ID_DESCRIPTION)],
        week: Annotated[int, Field(description=WEEK_DESCRIPTION)] | None = None,
    ) -> list[dict[str, str]]:
        """查询指定学生的课程安排（课表）。

        Args:
            student_id: 学生学号，例如 ``student-001``。
            week: 教学周序号（``1``–``30``）；``None`` 表示不限教学周。

        Returns:
            课程记录列表，每项含 ``course`` / ``teacher`` / ``weekday`` /
            ``time`` / ``room`` / ``weeks``。查不到课返回**空列表**（不是错误）。

        Raises:
            ToolError: ``week`` 越界、业务服务不可用 / 拒绝请求 / 内部错误，
                或返回结构异常。**必须**用 ``ToolError`` 而不是任意异常——
                按 MCP SDK 的约定，只有 ``ToolError`` 会把原始消息交给模型；
                其它异常一律被替换成 ``Error executing tool <name>``，
                业务原因就到不了 Agent，用户只会看到一句无意义的英文。
        """

        logger.info(
            "MCP 工具调用: %s student_id=%s week=%s",
            TOOL_GET_COURSE_SCHEDULE,
            student_id,
            week,
        )
        try:
            # 与直连工具共用同一份周次口径（app.tools.course_contract）：
            # 两条链路上的「第 30 周」必须同时被接受、第 99 周必须同时被拒。
            target_week = validate_week(week)
        except (TypeError, ValueError) as exc:
            # 参数取值本身就是可预期的失败，交给模型让它改参重试，
            # 因此归到 ToolError 而不是让它变成「工具崩溃」。
            logger.warning("MCP 工具参数非法: %s week=%r", type(exc).__name__, week)
            raise ToolError(str(exc)) from exc

        try:
            rows: list[dict[str, Any]] = get_campus_api_client().fetch_course_schedule(
                student_id=student_id, week=target_week
            )
            normalized = normalize_course_rows(rows)
        except CampusApiError as exc:
            # 可预期的业务失败（服务不可达 / 4xx / 5xx / 数据格式异常）：
            # CampusApiError 家族的 str() 已经是可直接转述给用户的中文，
            # 这里原样交给工具错误通道，让模型能据此向用户解释。
            logger.warning("MCP 工具业务失败: %s: %s", type(exc).__name__, exc)
            raise ToolError(str(exc)) from exc
        logger.info("MCP 工具返回 %d 条课程记录", len(normalized))
        return normalized

    @server.tool(name=TOOL_GET_SCORE, description=SCORE_TOOL_DESCRIPTION)
    def get_score(
        student_id: Annotated[str, Field(description=STUDENT_ID_DESCRIPTION)],
        semester: Annotated[str, Field(description=SEMESTER_DESCRIPTION)] | None = None,
        course: Annotated[str, Field(description=COURSE_NAME_DESCRIPTION)] | None = None,
    ) -> list[dict[str, str | float]]:
        """查询指定学生的考试成绩。

        Args:
            student_id: 学生学号，例如 ``student-001``。
            semester: 学期，形如 ``2025-2026-1``；``None`` 表示不限学期。
            course: 课程名称；``None`` 表示不限课程。匹配为双向包含（模糊）。

        Returns:
            成绩记录列表，每项含 ``semester`` / ``course`` / ``score`` / ``credit``，
            其中 ``score`` 与 ``credit`` 是**数值**（不是字符串）。查不到返回**空列表**。

        Raises:
            ToolError: ``semester`` / ``course`` 类型非法，业务服务不可用 / 拒绝请求 /
                内部错误，或返回结构异常。**必须**用 ``ToolError`` 而不是任意异常——
                按 MCP SDK 的约定，只有 ``ToolError`` 会把原始消息交给模型；
                其它异常一律被替换成 ``Error executing tool <name>``，
                业务原因就到不了 Agent，用户只会看到一句无意义的英文。
        """

        logger.info(
            "MCP 工具调用: %s student_id=%s semester=%s course=%s",
            TOOL_GET_SCORE,
            student_id,
            semester,
            course,
        )
        try:
            # 与直连工具共用同一份参数口径（app.tools.score_contract）：
            # 两条链路上的「空串视为未提供」「剔除不可见字符」必须完全一致。
            target_semester = normalize_optional_text(semester, name="semester")
            target_course = normalize_optional_text(course, name="course")
        except TypeError as exc:
            # 参数类型本身就是可预期的失败，交给模型让它改参重试，
            # 因此归到 ToolError 而不是让它变成「工具崩溃」。
            logger.warning("MCP 工具参数非法: %s", type(exc).__name__)
            raise ToolError(str(exc)) from exc

        try:
            # 学期过滤在服务端完成；course 不进请求（成绩接口不提供该参数）。
            rows: list[dict[str, Any]] = get_campus_api_client().fetch_scores(
                student_id=student_id, semester=target_semester
            )
            normalized = normalize_score_rows(rows)
        except CampusApiError as exc:
            # 可预期的业务失败（服务不可达 / 4xx / 5xx / 数据格式异常）：
            # CampusApiError 家族的 str() 已经是可直接转述给用户的中文，
            # 这里原样交给工具错误通道，让模型能据此向用户解释。
            logger.warning("MCP 工具业务失败: %s: %s", type(exc).__name__, exc)
            raise ToolError(str(exc)) from exc

        # 课程名匹配留在本进程内（与直连工具同一个函数），不污染请求参数。
        selected = select_by_course(normalized, target_course)
        logger.info("MCP 工具返回 %d 条成绩记录", len(selected))
        return selected

    @server.tool(name=TOOL_SUBMIT_LEAVE_APPLICATION, description=LEAVE_TOOL_DESCRIPTION)
    def submit_leave_application(
        student_id: Annotated[str, Field(description=STUDENT_ID_DESCRIPTION)],
        start_time: Annotated[str, Field(description=START_TIME_DESCRIPTION)],
        end_time: Annotated[str, Field(description=END_TIME_DESCRIPTION)],
        reason: Annotated[str, Field(description=REASON_DESCRIPTION)],
    ) -> dict[str, str]:
        """提交指定学生的请假申请，返回新建的申请记录。

        Args:
            student_id: 学生学号，例如 ``student-001``。
            start_time: 请假开始时间，ISO 8601 文本（``2026-09-28 08:00``）。
            end_time: 请假结束时间，须不早于 ``start_time``。
            reason: 请假事由。

        Returns:
            新建的申请记录，含 ``id`` / ``studentId`` / ``startTime`` /
            ``endTime`` / ``reason`` / ``status`` / ``createdAt``；
            ``status`` 为 ``PENDING``（待审核）。

        Raises:
            ToolError: 参数空 / 格式无法解析 / 时间倒置，业务服务不可用、
                拒绝提交（4xx）、内部错误，或返回结构异常。
                **必须**用 ``ToolError`` 而不是任意异常——按 MCP SDK 的约定，
                只有 ``ToolError`` 会把原始消息交给模型；其它异常一律被替换成
                ``Error executing tool <name>``，业务原因就到不了 Agent。
                写操作尤其需要这条：模型必须看到「哪一项不合法」才能改参重来，
                而不是以为申请已经提交成功。
        """

        logger.info(
            "MCP 工具调用: %s student_id=%s start_time=%s end_time=%s",
            TOOL_SUBMIT_LEAVE_APPLICATION,
            student_id,
            start_time,
            end_time,
        )
        try:
            # 与将来的直连工具共用同一份参数口径（app.tools.leave_contract）：
            # 「空事由算缺失」「时间怎么算合法」只在这里解释一次。
            target_start = normalize_datetime(start_time, name="请假开始时间")
            target_end = normalize_datetime(end_time, name="请假结束时间")
            target_reason = normalize_required_text(reason, name="请假事由")
        except (TypeError, ValueError) as exc:
            # 参数取值本身就是可预期的失败，交给模型让它改参重试，
            # 因此归到 ToolError 而不是让它变成「工具崩溃」。
            logger.warning("MCP 工具参数非法: %s: %s", type(exc).__name__, exc)
            raise ToolError(str(exc)) from exc

        try:
            # 终态由服务端决定（数据库默认 PENDING），本进程不预设、不覆盖。
            record: dict[str, Any] = get_campus_api_client().submit_leave_application(
                start_time=target_start,
                end_time=target_end,
                reason=target_reason,
                student_id=student_id,
            )
            normalized = normalize_leave_result(record)
        except CampusApiError as exc:
            # 可预期的业务失败（服务不可达 / 4xx / 5xx / 数据格式异常）：
            # CampusApiError 家族的 str() 已经是可直接转述给用户的中文，
            # 这里原样交给工具错误通道，让模型能据此向用户解释或改参重试。
            logger.warning("MCP 工具业务失败: %s: %s", type(exc).__name__, exc)
            raise ToolError(str(exc)) from exc

        logger.info(
            "MCP 工具提交成功: %s id=%s status=%s",
            TOOL_SUBMIT_LEAVE_APPLICATION,
            normalized["id"],
            normalized["status"],
        )
        return normalized

    return server


def _configure_logging() -> None:
    """把日志固定到 stderr——stdout 是 MCP 协议通道，绝不能被日志污染。"""

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def main() -> None:
    """进程入口：以 stdio transport 运行 MCP Server（由 MCP Client 拉起）。"""

    _configure_logging()
    logger.info("启动 %s（transport=stdio，stdout 为协议通道）", SERVER_NAME)
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = [
    "COURSE_NAME_DESCRIPTION",
    "COURSE_TOOL_DESCRIPTION",
    "END_TIME_DESCRIPTION",
    "EXAM_TOOL_DESCRIPTION",
    "LEAVE_TOOL_DESCRIPTION",
    "REASON_DESCRIPTION",
    "SCORE_TOOL_DESCRIPTION",
    "SEMESTER_DESCRIPTION",
    "SERVER_NAME",
    "START_TIME_DESCRIPTION",
    "STUDENT_ID_DESCRIPTION",
    "TOOL_GET_COURSE_SCHEDULE",
    "TOOL_GET_EXAM_SCHEDULE",
    "TOOL_GET_SCORE",
    "TOOL_SUBMIT_LEAVE_APPLICATION",
    "WEEK_DESCRIPTION",
    "build_server",
    "main",
]
