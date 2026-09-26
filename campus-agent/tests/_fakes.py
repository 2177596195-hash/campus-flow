"""测试共享替身与工厂（检索层 + Agent 层 + 外部服务层）。

设计原则：替身只实现 LangChain / LangGraph 的抽象，
不引入任何具体向量库类型，也不发起真实网络请求，因此测试可完全离线、可重现地运行。

- 检索层：``FakeVectorStore`` / ``ScriptedVectorStore`` / ``HashingEmbeddings`` …
- Agent 层：``ScriptedChatModel``（可确定性地产生 ``tool_calls``）、
  ``StaticIntentClassifier``、``FakeRAGService``
- 外部服务层：``make_campus_client``（挂在 ``httpx.MockTransport`` 上的
  校园业务服务客户端替身，可按路径路由 ``/api/exams`` / ``/api/courses`` /
  ``/api/scores`` / ``/api/leaves``）、``make_campus_api_settings``、
  ``patch_campus_api_for_tools``
- MCP 层：``FakeMCPClient``、``make_mcp_tool_definition``、``make_mcp_settings``

关于 HTTP 替身：这里**不用** mock 库替换整个 HTTP 层，而是给 ``httpx.Client``
挂一个 ``MockTransport``。好处是 ``CampusApiClient`` 的代码路径被真实执行
（URL 拼接、query 参数、状态码分支、超时异常、JSON 解析），只有「数据包从哪来」
被替换掉，因此断言的确定性不会以牺牲覆盖率为代价。

关于 MCP 替身：``FakeMCPClient`` 实现与 ``StdioMCPClient`` **相同的公开方法**
（``start`` / ``list_tools`` / ``find_tool`` / ``call_tool``），因此适配层与 Agent
用例既不需要起子进程，也不需要 MCP Server——需要真实 stdio 往返的用例放在
``tests/mcp/test_client_stdio.py``，它用本机回环 stub HTTP 服务替代 Spring Boot。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any, Final

import httpx
import mcp.types as mcp_types
import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.vectorstores import VectorStore
from pydantic import Field

from app.agent.intent import Intent
from app.config.settings import (
    CampusApiSettings,
    EmbeddingSettings,
    MCPSettings,
    RAGSettings,
)
from app.infrastructure.campus_api import (
    COURSE_SCHEDULE_PATH,
    EXAM_SCHEDULE_PATH,
    LEAVE_PATH,
    SCORE_PATH,
    CampusApiClient,
)
from app.rag.schema import RAGAnswer

#: 距离 -> 相关性 的换算函数，语义与 langchain_core 的余弦约定一致
CosineRelevanceFn = Callable[[float], float]


def cosine_relevance(distance: float) -> float:
    """余弦空间的距离换算：距离越小相关性越高，结果落在 ``[0, 1]``。"""

    return 1.0 - distance


def make_document(
    content: str,
    *,
    source: str = "campus_rules.md",
    title: str = "校园规章",
    page: int | None = None,
    **extra: Any,
) -> Document:
    """构造带来源 metadata 的测试文档。"""

    metadata: dict[str, Any] = {"source": source, "title": title}
    if page is not None:
        metadata["page"] = page
    metadata.update(extra)
    return Document(page_content=content, metadata=metadata)


def make_rag_settings(*, top_k: int = 4, score_threshold: float | None = None) -> RAGSettings:
    """构造一份无需读取环境变量的 RAG 配置，供检索器测试注入。"""

    return RAGSettings(
        knowledge_dir=Path("/tmp/campus-knowledge"),
        persist_dir=Path("/tmp/campus-vectorstore"),
        collection_name="test_collection",
        vectorstore_backend="chroma",
        chunk_size=500,
        chunk_overlap=80,
        top_k=top_k,
        score_threshold=score_threshold,
        history_window=6,
        auto_ingest=False,
        embedding=EmbeddingSettings(
            provider="test",
            api_key="dummy-key-for-test",
            base_url="http://127.0.0.1:1",
            model="test-embedding",
            dimensions=None,
            batch_size=10,
            supports_token_array_input=False,
            timeout=1.0,
            max_retries=0,
        ),
    )


def make_ingest_settings(
    *,
    knowledge_dir: Path,
    persist_dir: Path,
    chunk_size: int = 500,
    chunk_overlap: int = 80,
    manifest_path: Path | None = None,
) -> RAGSettings:
    """构造增量索引测试用的配置：目录指向临时路径，不读取任何环境变量。

    ``manifest_path`` 省略时由 ``resolve_manifest_path`` 落到 ``persist_dir`` 内，
    与生产默认行为一致。
    """

    return replace(
        make_rag_settings(),
        knowledge_dir=knowledge_dir,
        persist_dir=persist_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        manifest_path=manifest_path,
    )


# ------------------------------------------------- 校园业务服务（HTTP）替身

#: 替身服务基址。刻意不用 8080：万一本机恰好开着真实服务，
#: 也不会因为「地址碰巧对上了」而让测试结果失真。
CAMPUS_TEST_BASE_URL: str = "http://campus.test"

#: 与 MySQL 种子数据一致的考试安排响应。
#: 内容与 ``campus-service/src/main/resources/data.sql`` 中 student-001 的记录一致，
#: 因此 Agent 层用例既能离线运行，断言的又是真实契约下的数据形态
#: （而不是一份为了测试方便而虚构的结构）。
EXAM_SCHEDULE_PAYLOAD: list[dict[str, str]] = [
    {"course": "计算机网络", "date": "2026-09-28", "time": "14:00", "room": "A302"},
    {"course": "数据结构", "date": "2026-09-30", "time": "19:00", "room": "B201"},
]

#: 星期序号 -> 中文标签，与 Spring Boot 的 ``CourseScheduleResponse`` 一致。
WEEKDAY_LABELS: Final[tuple[str, ...]] = (
    "", "周一", "周二", "周三", "周四", "周五", "周六", "周日"
)

#: 与 MySQL 种子数据一致的课表（student-001）。
#:
#: 内容与 ``campus-service/src/main/resources/data.sql`` 中 student-001 的行一致，
#: 但刻意保持**结构化**形态（weekday 用序号、周次用区间），渲染规则与服务端
#: ``CourseScheduleResponse`` 完全相同：序号 -> 「周一」、区间 -> 「1-16周」。
#: 这样 Agent 层用例既离线运行，断言的又是真实契约下的数据形态；
#: 若把展示串直接写死在这里，服务端渲染规则一改，测试反而察觉不到。
#:
#: 「数据库原理」第 5 周才开课，用于让「按周过滤真的生效」可被直接断言
#: （第 3 周查不到、第 8 周查得到），而不是只看条数变化。
COURSE_SCHEDULE_TABLE: list[dict[str, Any]] = [
    {"course": "数据结构", "teacher": "李明", "weekday": 1,
     "time": "08:00-09:40", "room": "A302", "week_start": 1, "week_end": 16},
    {"course": "计算机网络", "teacher": "王芳", "weekday": 1,
     "time": "14:00-15:40", "room": "B201", "week_start": 1, "week_end": 16},
    {"course": "高等数学", "teacher": "张伟", "weekday": 2,
     "time": "08:00-09:40", "room": "C105", "week_start": 1, "week_end": 18},
    {"course": "大学英语", "teacher": "刘洋", "weekday": 3,
     "time": "10:00-11:40", "room": "D202", "week_start": 1, "week_end": 16},
    {"course": "操作系统", "teacher": "陈静", "weekday": 3,
     "time": "14:00-15:40", "room": "A401", "week_start": 1, "week_end": 16},
    {"course": "Java程序设计", "teacher": "孙浩", "weekday": 4,
     "time": "08:00-09:40", "room": "E101", "week_start": 1, "week_end": 16},
    {"course": "数据结构", "teacher": "李明", "weekday": 4,
     "time": "10:00-11:40", "room": "A302", "week_start": 2, "week_end": 16},
    {"course": "数据库原理", "teacher": "赵磊", "weekday": 5,
     "time": "08:00-09:40", "room": "B305", "week_start": 5, "week_end": 16},
]


def course_schedule_payload(*, week: int | None = None) -> list[dict[str, str]]:
    """按 Spring Boot ``/api/courses`` 的契约渲染课表。

    ``week`` 为 ``None`` 时返回整学期课表；给出时只保留「覆盖该周」的课程
    （``week_start <= week <= week_end``），与服务端 ``CourseService`` 的
    过滤条件一致。
    """

    return [
        {
            "course": item["course"],
            "teacher": item["teacher"],
            "weekday": WEEKDAY_LABELS[int(item["weekday"])],
            "time": item["time"],
            "room": item["room"],
            "weeks": f"{item['week_start']}-{item['week_end']}周",
        }
        for item in COURSE_SCHEDULE_TABLE
        if week is None or int(item["week_start"]) <= week <= int(item["week_end"])
    ]


#: 整学期课表（不按周过滤），即 ``/api/courses`` 不带 ``week`` 时的响应。
COURSE_SCHEDULE_PAYLOAD: list[dict[str, str]] = course_schedule_payload()


def course_stub_response(request: httpx.Request) -> list[dict[str, str]]:
    """替身服务端：从 query 里取 ``week`` 并按周过滤。

    为什么要读 query 而不是「返回一份固定课表」：``get_course_schedule`` 的
    ``week`` 现在由**服务端**过滤，Python 侧只负责透传。若替身对任何
    ``week`` 都返回同一份数据，就无法区分「参数真的传下去了」与
    「参数被忽略了」——这正是最容易悄悄坏掉的地方。
    """

    raw_week = request.url.params.get("week")
    return course_schedule_payload(week=int(raw_week) if raw_week else None)


#: 与 MySQL 种子数据一致的成绩表（student-001）。
#:
#: 内容与 ``campus-service/src/main/resources/data.sql`` 中 student-001 的行一致，
#: 刻意保持**结构化**形态；渲染顺序与服务端 ``ScoreRepository`` 的排序相同：
#: 学期从新到旧、同学期按课程名。这样 Agent 层用例既离线运行，
#: 断言的又是真实契约下的数据形态。
#:
#: 覆盖两个学期（2025-2026-1 三门、2024-2025-2 两门），使「按学期过滤真的生效」
#: 可被直接断言（查上学期只应拿到两门，而不是全部五门），而不是只看条数变化。
SCORE_TABLE: list[dict[str, Any]] = [
    {"semester": "2025-2026-1", "course": "数据结构", "score": 92.0, "credit": 4.0},
    {"semester": "2025-2026-1", "course": "计算机网络", "score": 88.0, "credit": 3.0},
    {"semester": "2025-2026-1", "course": "操作系统", "score": 95.0, "credit": 3.5},
    {"semester": "2024-2025-2", "course": "高等数学", "score": 84.0, "credit": 5.0},
    {"semester": "2024-2025-2", "course": "大学英语", "score": 86.0, "credit": 2.0},
]


def score_payload(*, semester: str | None = None) -> list[dict[str, Any]]:
    """按 Spring Boot ``/api/scores`` 的契约渲染成绩。

    ``semester`` 为 ``None`` 时返回全部成绩；给出时只保留该学期
    （精确匹配，与服务端 ``ScoreRepository`` 的 ``semester = ?`` 一致）。
    排序与服务端一致：学期从新到旧、同学期按课程名升序（先按课程名排一次，
    再按学期稳定排序，从而保留同学期内的课程名次序）。
    """

    rows = [
        dict(row)
        for row in SCORE_TABLE
        if semester is None or row["semester"] == semester
    ]
    by_course = sorted(rows, key=lambda row: str(row["course"]))
    return sorted(by_course, key=lambda row: str(row["semester"]), reverse=True)


#: 全部成绩（不按学期过滤），即 ``/api/scores`` 不带 ``semester`` 时的响应。
SCORE_PAYLOAD: list[dict[str, Any]] = score_payload()


def score_stub_response(request: httpx.Request) -> list[dict[str, Any]]:
    """替身服务端：从 query 里取 ``semester`` 并按学期过滤。

    为什么要读 query 而不是「返回一份固定成绩」：``semester`` 的过滤是**服务端**
    的职责，Python 侧只负责透传。若替身对任何 ``semester`` 都返回同一份数据，
    就无法区分「参数真的传下去了」与「参数被忽略了」——这正是最容易悄悄坏掉的地方。
    """

    raw_semester = request.url.params.get("semester")
    return score_payload(semester=raw_semester or None)


#: 请假提交成功的响应，与 Spring Boot ``LeaveApplicationResponse`` 的字段一致。
#:
#: 与前三个查询替身不同，这里刻意**不**做「与 MySQL 种子数据一致」的对齐——
#: 请假是追加型事务数据，库里本来就没有（也不该有）预置记录，
#: 申请只应由真实的提交动作产生。因此这份负载描述的是「服务端收到提交后会返回
#: 什么形状」，而不是「库里已经有什么」。
LEAVE_APPLICATION_PAYLOAD: dict[str, Any] = {
    "id": 1,
    "studentId": "student-001",
    "startTime": "2026-10-12T08:00",
    "endTime": "2026-10-14T18:00",
    "reason": "感冒发烧需就医",
    "status": "PENDING",
    "createdAt": "2026-10-12T08:03",
}


def leave_stub_response(request: httpx.Request) -> dict[str, Any]:
    """替身服务端：回显请求体并补上服务端生成的字段。

    为什么要读**请求体**（而不是返回一份固定记录）：写下这条链路时最容易悄悄坏掉
    的地方是「参数到底有没有传下去」——请假有三个由模型填写的字段
    （``start_time`` / ``end_time`` / ``reason``）与一个由宿主绑定的字段
    （``student_id``），只断言「工具被调用了」无法区分它们是否真的进了请求。
    回显让「请求体长什么样」成为可精确断言的等式。
    """

    try:
        body = json.loads(request.content.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, Mapping):
        body = {}
    return {
        "id": 1,
        "studentId": body.get("studentId", ""),
        "startTime": body.get("startTime", ""),
        "endTime": body.get("endTime", ""),
        "reason": body.get("reason", ""),
        "status": "PENDING",
        "createdAt": "2026-10-12T08:03",
    }


#: 离线替身的默认路由表：路径 -> 响应负载（也可以是 ``request -> 负载`` 的函数）。
#: 键直接取自生产常量，避免这里再抄一份路径字面量。
DEFAULT_CAMPUS_PAYLOADS: dict[str, object] = {
    EXAM_SCHEDULE_PATH: EXAM_SCHEDULE_PAYLOAD,
    COURSE_SCHEDULE_PATH: course_stub_response,
    SCORE_PATH: score_stub_response,
    LEAVE_PATH: leave_stub_response,
}


def make_campus_api_settings(
    *,
    base_url: str = CAMPUS_TEST_BASE_URL,
    timeout: float = 1.0,
    student_id: str = "student-001",
) -> CampusApiSettings:
    """构造一份无需读取环境变量的校园业务服务配置。"""

    return CampusApiSettings(base_url=base_url, timeout=timeout, default_student_id=student_id)


def make_campus_client(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    payload: object | None = None,
    payloads: Mapping[str, object] | None = None,
    status_code: int = 200,
    raw_body: str | None = None,
    base_url: str = CAMPUS_TEST_BASE_URL,
    timeout: float = 1.0,
    default_student_id: str = "student-001",
    requests: list[httpx.Request] | None = None,
) -> CampusApiClient:
    """构造挂在 ``httpx.MockTransport`` 上的校园业务服务客户端。

    Args:
        handler: 完全自定义的响应函数（优先级最高）。用于模拟超时、连接失败
            这类无法用「返回一个 Response」表达的场景。
        payload: 所有路径统一返回的 JSON 负载；``None`` 时用
            ``EXAM_SCHEDULE_PAYLOAD``。与 ``payloads`` 二选一。
        payloads: 按**请求路径**分派的负载表（键为路径，值为 JSON 负载，或
            ``request -> 负载`` 的函数）。考试与课程是两个接口，
            统一 payload 表达不了它们各自的响应，因此需要这一项；
            传 ``DEFAULT_CAMPUS_PAYLOADS`` 即可让 ``/api/exams`` 与
            ``/api/courses`` 都返回与 MySQL 种子数据一致的响应。
            未登记的路径返回空数组。
        status_code: 默认响应状态码。
        raw_body: 非 JSON 响应体，用于验证解析失败分支。
        requests: 传入列表即可记录收到的全部请求，用于断言 URL 与 query 参数。

    Returns:
        可直接注入各工具模块（``exam_tools`` / ``course_tools``）的客户端。
    """

    seen: list[httpx.Request] = requests if requests is not None else []
    default_payload = EXAM_SCHEDULE_PAYLOAD if payload is None else payload
    routes = None if payloads is None else dict(payloads)

    def default_handler(request: httpx.Request) -> httpx.Response:
        if raw_body is not None:
            return httpx.Response(status_code, text=raw_body)
        if routes is None:
            return httpx.Response(status_code, json=default_payload)
        entry = routes.get(request.url.path, [])
        data = entry(request) if callable(entry) else entry
        return httpx.Response(status_code, json=data)

    inner = handler or default_handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner(request)

    return CampusApiClient(
        base_url,
        timeout=timeout,
        default_student_id=default_student_id,
        client=httpx.Client(transport=httpx.MockTransport(recording_handler)),
    )


def patch_campus_api(monkeypatch: pytest.MonkeyPatch, client: CampusApiClient) -> CampusApiClient:
    """把替身客户端注入 ``exam_tools``。

    工具内部读的是模块全局名 ``get_campus_api_client``，因此替换模块属性即可生效，
    不需要在生产代码里专门开一个「测试专用 setter」。
    """

    from app.tools import exam_tools

    monkeypatch.setattr(exam_tools, "get_campus_api_client", lambda: client)
    return client


def patch_campus_api_in(
    monkeypatch: pytest.MonkeyPatch, module: Any, client: CampusApiClient
) -> CampusApiClient:
    """把替身客户端注入**指定模块**。

    每个模块都是「按名字导入 ``get_campus_api_client``」，因此要在它自己的命名空间里
    替换才会生效。MCP Server（``app.mcp.server``）与工具层是两个独立的导入点，
    所以这里显式区分，避免出现「以为替换了、其实没生效」的假绿用例。
    """

    monkeypatch.setattr(module, "get_campus_api_client", lambda: client)
    return client


#: 会「按名字导入 ``get_campus_api_client``」的工具模块。
#:
#: 新增工具模块时必须在此登记。漏登记的后果不会报错——用例会**悄悄**变成
#: 「去连本机 8081 上真实运行的服务」，在没有服务的机器上失败、在本机碰巧
#: 开着服务时假绿。因此 ``tests/tools`` 里有一条守卫用例比对这份名单与
#: 实际调用该函数的模块集合。
TOOL_MODULES_WITH_CAMPUS_CLIENT: Final[tuple[str, ...]] = (
    "app.tools.exam_tools",
    "app.tools.course_tools",
    "app.tools.score_tools",
)


def patch_campus_api_for_tools(
    monkeypatch: pytest.MonkeyPatch, client: CampusApiClient
) -> CampusApiClient:
    """把替身客户端注入**全部**使用它的工具模块。

    每个模块都是「按名字导入」，因此要在各自的命名空间里替换才会生效。
    新增工具模块时只需登记到 ``TOOL_MODULES_WITH_CAMPUS_CLIENT``。
    """

    for module_name in TOOL_MODULES_WITH_CAMPUS_CLIENT:
        patch_campus_api_in(monkeypatch, import_module(module_name), client)
    return client


# ------------------------------------------------------------------ MCP 替身

#: 替身 MCP 工具名（与真实 Server 注册的名字一致，便于断言「发现结果被用上了」）
MCP_TOOL_NAME: str = "get_exam_schedule"

#: 替身 MCP 工具描述：形态与真实 Server 的描述一致（含字段名与实时性说明）
MCP_TOOL_DESCRIPTION: str = (
    "查询指定学生的近期考试安排。数据来自校园教务系统的实时查询（不是本地固定数据）。"
    "返回该学生的全部考试安排，按考试时间升序；每项含四个字段："
    "course（考试科目）、date（考试日期 yyyy-MM-dd）、"
    "time（开考时间 HH:mm）、room（考场）。"
)

#: 替身 MCP 工具的 ``inputSchema``。
#: 直接抄自 mcp 2.2.0 由函数签名生成的真实形态（含 ``required`` 与 ``title``），
#: 这样适配层的 schema 生成逻辑面对的是真实数据形状而不是简化版。
MCP_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "properties": {"student_id": {"title": "Student Id", "type": "string"}},
    "required": ["student_id"],
    "type": "object",
    "title": "get_exam_scheduleArguments",
}


def make_mcp_tool_definition(
    *,
    name: str = MCP_TOOL_NAME,
    description: str = MCP_TOOL_DESCRIPTION,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
) -> mcp_types.Tool:
    """构造 ``tools/list`` 会返回的工具定义。"""

    return mcp_types.Tool(
        name=name,
        description=description,
        input_schema=dict(MCP_TOOL_INPUT_SCHEMA if input_schema is None else input_schema),
        output_schema=None if output_schema is None else dict(output_schema),
    )


#: 替身课程 MCP 工具名（与真实 Server 注册的名字一致）
MCP_COURSE_TOOL_NAME: str = "get_course_schedule"

#: 替身课程 MCP 工具描述：形态与真实 Server 的描述一致（含字段名、实时性与周次说明）
MCP_COURSE_TOOL_DESCRIPTION: str = (
    "查询学生的课程安排（课表），返回课程名称、任课教师、星期、时间、教室与上课周次。"
    "数据来自校园教务系统的实时查询（不是本地固定数据）。"
    "返回结果按星期、上课时间升序；每项含六个字段："
    "course（课程名称）、teacher（任课教师）、weekday（星期）、"
    "time（上课时段）、room（教室）、weeks（上课周次区间，如 1-16周）。"
)

#: 替身课程 MCP 工具的 ``inputSchema``。
#: 直接抄自 mcp 2.2.0 对 ``week: int | None = None`` 生成的真实形态：可选参数
#: 会变成 ``anyOf(integer|null)``，``description`` 嵌在分支里、顶层只有 ``default``。
#: 适配层的 schema 生成必须面对这个真实形状，否则「可选参数被降级成 Any 且丢说明」
#: 这类缺陷在替身里根本不会被触发。
MCP_COURSE_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "properties": {
        "student_id": {
            "description": "学生学号，例如 student-001",
            "title": "Student Id",
            "type": "string",
        },
        "week": {
            "anyOf": [
                {
                    "description": (
                        "教学周序号，例如 3 表示第 3 教学周。当前为第 3 教学周，"
                        "用户说的「这周 / 本周」即该值；省略时不限教学周，返回整学期课表。"
                    ),
                    "type": "integer",
                },
                {"type": "null"},
            ],
            "default": None,
            "title": "Week",
        },
    },
    "required": ["student_id"],
    "title": "get_course_scheduleArguments",
    "type": "object",
}


def make_mcp_course_tool_definition(
    *,
    name: str = MCP_COURSE_TOOL_NAME,
    description: str = MCP_COURSE_TOOL_DESCRIPTION,
    input_schema: Mapping[str, Any] | None = None,
) -> mcp_types.Tool:
    """构造 ``tools/list`` 会返回的**课程**工具定义。"""

    return mcp_types.Tool(
        name=name,
        description=description,
        input_schema=dict(
            MCP_COURSE_TOOL_INPUT_SCHEMA if input_schema is None else input_schema
        ),
    )


#: 替身成绩 MCP 工具名（与真实 Server 注册的名字一致）
MCP_SCORE_TOOL_NAME: str = "get_score"

#: 替身成绩 MCP 工具描述：形态与真实 Server 的描述一致（含四字段、实时性、
#: 以及「分数与学分是数字」这条会影响模型作答方式的提示）
MCP_SCORE_TOOL_DESCRIPTION: str = (
    "查询学生的考试成绩，返回学期、课程名称、成绩分数与学分。"
    "数据来自校园教务系统的实时查询（不是本地固定数据）。"
    "每项含四个字段：semester（学期，形如 2025-2026-1）、course（课程名称）、"
    "score（成绩分数，数值）、credit（学分，数值）；分数与学分是数字，"
    "可直接比较大小与做加权计算。"
)

#: 替身成绩 MCP 工具的 ``inputSchema``。
#: 直接抄自 mcp 2.2.0 对 ``semester: str | None = None`` / ``course: str | None = None``
#: 生成的真实形态：两个可选参数都是 ``anyOf(string|null)``，``description`` 嵌在分支里、
#: 顶层只有 ``default``。适配层必须解析这种形态，否则这两个可选参数会被降级成
#: 无类型、无说明的 ``Any``，模型就失去「当前是哪个学期」这条关键提示。
MCP_SCORE_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "properties": {
        "student_id": {
            "description": "学生学号，例如 student-001",
            "title": "Student Id",
            "type": "string",
        },
        "semester": {
            "anyOf": [
                {
                    "description": (
                        "学期，形如 2025-2026-1。当前为 2025-2026-1 学期，"
                        "用户说的「这个学期 / 本学期」即该值；省略时不限学期，返回全部成绩。"
                    ),
                    "type": "string",
                },
                {"type": "null"},
            ],
            "default": None,
            "title": "Semester",
        },
        "course": {
            "anyOf": [
                {
                    "description": (
                        "课程名称，例如「微观经济学」；省略时返回该范围内全部课程。"
                        "支持模糊匹配（课程名双向包含）。"
                    ),
                    "type": "string",
                },
                {"type": "null"},
            ],
            "default": None,
            "title": "Course",
        },
    },
    "required": ["student_id"],
    "title": "get_scoreArguments",
    "type": "object",
}


def make_mcp_score_tool_definition(
    *,
    name: str = MCP_SCORE_TOOL_NAME,
    description: str = MCP_SCORE_TOOL_DESCRIPTION,
    input_schema: Mapping[str, Any] | None = None,
) -> mcp_types.Tool:
    """构造 ``tools/list`` 会返回的**成绩**工具定义。"""

    return mcp_types.Tool(
        name=name,
        description=description,
        input_schema=dict(
            MCP_SCORE_TOOL_INPUT_SCHEMA if input_schema is None else input_schema
        ),
    )


#: 替身请假 MCP 工具名（与真实 Server 注册的名字一致）
MCP_LEAVE_TOOL_NAME: str = "submit_leave_application"

#: 替身请假 MCP 工具描述：形态与真实 Server 的描述一致——**首要任务是划清
#: 「提交」与「查询」的界限**（写操作无法像查询那样「答错也没关系」）。
MCP_LEAVE_TOOL_DESCRIPTION: str = (
    "提交学生的请假申请（写操作：会真实创建一条请假记录并进入待审核状态）。"
    "当用户表达「我要请假」「帮我请个假」「提交请假申请」这类**提交**意图时调用本工具；"
    "请先确认请假的开始时间、结束时间与事由都已明确，缺哪项就先向用户询问，"
    "不要自行编造时间或事由。"
    "本工具只负责**提交**：不能查询已有请假记录、不能审批（通过 / 驳回）、"
    "不能撤销或修改。用户只是询问请假制度、流程或结果时不要调用本工具。"
    "提交成功后返回申请编号 id、当前状态 status（待审核）、以及起止时间与事由，"
    "请把申请编号与状态告知用户。"
)

#: 替身请假 MCP 工具的 ``inputSchema``。
#: 三个由模型填写的参数都是**必填**（与真实 Server 的签名一致：没有默认值），
#: ``student_id`` 同为必填但会在适配层被绑定、不进模型可见的 schema——
#: 「三个用户参数 + 一个身份参数」这个形状必须被如实反映，否则适配层的
#: 「绑定参数被摘掉、其余参数保留且必填」这条行为在替身里根本不会被触发。
MCP_LEAVE_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "properties": {
        "student_id": {
            "description": "学生学号，例如 student-001",
            "title": "Student Id",
            "type": "string",
        },
        "start_time": {
            "description": (
                "请假开始时间，形如 2026-10-12 08:00，例如 2026-10-12 08:00。"
                "必须是明确的日期与时刻，不要只说「明天」这类相对说法。"
            ),
            "title": "Start Time",
            "type": "string",
        },
        "end_time": {
            "description": (
                "请假结束时间，形如 2026-10-12 08:00，例如 2026-10-14 18:00。"
                "必须晚于或等于开始时间。"
            ),
            "title": "End Time",
            "type": "string",
        },
        "reason": {
            "description": (
                "请假事由，例如「感冒发烧需就医」。必填，请用用户原话概括，"
                "不要留空、不要编造。"
            ),
            "title": "Reason",
            "type": "string",
        },
    },
    "required": ["student_id", "start_time", "end_time", "reason"],
    "title": "submit_leave_applicationArguments",
    "type": "object",
}


def make_mcp_leave_tool_definition(
    *,
    name: str = MCP_LEAVE_TOOL_NAME,
    description: str = MCP_LEAVE_TOOL_DESCRIPTION,
    input_schema: Mapping[str, Any] | None = None,
) -> mcp_types.Tool:
    """构造 ``tools/list`` 会返回的**请假提交**工具定义。"""

    return mcp_types.Tool(
        name=name,
        description=description,
        input_schema=dict(
            MCP_LEAVE_TOOL_INPUT_SCHEMA if input_schema is None else input_schema
        ),
    )


def make_mcp_server_tool_definitions() -> tuple[mcp_types.Tool, ...]:
    """当前 MCP Server 应有的全部工具定义（与 ``app.mcp.server.build_server()`` 对齐）。

    顺序与 ``build_server()`` 的注册顺序一致，因此可直接用于断言「三个查询槽位
    都被替换、请假槽位被追加」。**四个工具缺一不可**：若这里少登记一个，
    基于它的装配用例就会在「服务端没提供该工具」的回退路径上假绿。
    """

    return (
        make_mcp_tool_definition(),
        make_mcp_course_tool_definition(),
        make_mcp_score_tool_definition(),
        make_mcp_leave_tool_definition(),
    )


def make_mcp_settings(
    *,
    enabled: bool = True,
    server_name: str = "campus-mcp-server",
    command: str = "python",
    args: Sequence[str] = ("-m", "app.mcp.server"),
    cwd: Path | None = Path("/tmp/campus-agent"),
    startup_timeout: float = 5.0,
    call_timeout: float = 5.0,
) -> MCPSettings:
    """构造一份无需读取环境变量的 MCP 配置。"""

    return MCPSettings(
        enabled=enabled,
        server_name=server_name,
        command=command,
        args=tuple(args),
        cwd=cwd,
        startup_timeout=startup_timeout,
        call_timeout=call_timeout,
    )


class FakeMCPClient:
    """``StdioMCPClient`` 的离线替身：不起子进程、不连 Server。

    公开方法（``start`` / ``list_tools`` / ``find_tool`` / ``call_tool``）与真实
    实现**同名同签名**，因此适配层与 Agent 用例可以在完全没有 MCP 环境的前提下
    走完「发现 -> 生成 schema -> 调用」的完整路径。

    记录 ``started`` / ``calls``，用于断言两类容易写错的边界：

    - 工具清单该被解析时确实连了 Server（而不是悄悄回退成直连工具）；
    - RAG / chat 分支**一次都没有**碰过 MCP。
    """

    def __init__(
        self,
        *,
        tools: Sequence[mcp_types.Tool] | None = None,
        payload: object | None = None,
        payloads: Mapping[str, object] | None = None,
        start_error: BaseException | None = None,
        call_error: BaseException | None = None,
    ) -> None:
        self.tools: tuple[mcp_types.Tool, ...] = tuple(
            tools if tools is not None else (make_mcp_tool_definition(),)
        )
        self.payload = EXAM_SCHEDULE_PAYLOAD if payload is None else payload
        #: 按**工具名**分派的返回值。用于「同一条会话上不同工具返回不同结构」的场景
        #: （成绩四字段与课程六字段的形态不同，单一 ``payload`` 表达不了）。
        #: 未登记的工具有则退回 ``payload``。
        self.payloads: dict[str, object] = dict(payloads or {})
        self.start_error = start_error
        self.call_error = call_error
        #: ``start`` 被调用的次数（>0 表示确实尝试了建连）
        self.start_count = 0
        #: 依次收到的 ``(工具名, 参数)``
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.server_name = "campus-mcp-server"

    # ------------------------------------------------------------ 与真实客户端一致

    @property
    def started(self) -> bool:
        return self.start_count > 0 and self.start_error is None

    def start(self, timeout: float | None = None) -> FakeMCPClient:
        self.start_count += 1
        if self.start_error is not None:
            raise self.start_error
        return self

    def close(self) -> None:
        self.closed = True

    def list_tools(self) -> tuple[mcp_types.Tool, ...]:
        return self.tools

    def find_tool(self, name: str) -> mcp_types.Tool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        from app.mcp.client import MCPToolNotFound

        raise MCPToolNotFound(f"校园 MCP 服务未提供工具 {name}")

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        self.calls.append((name, dict(arguments or {})))
        if self.call_error is not None:
            raise self.call_error
        if name in self.payloads:
            return self.payloads[name]
        return self.payload

    async def acall_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> Any:
        return self.call_tool(name, arguments)


class FakeVectorStore(VectorStore):
    """离线替身：模拟底层返回 **距离** 的向量库（距离越小越相关）。

    ``hits`` 中每条是 ``(文档, 距离)``，且**故意允许乱序**，用于验证检索器
    不依赖后端返回顺序。三个开关用于模拟不同后端的真实差异：

    - ``supports_relevance_scores=False``：后端不直接提供相关性分数
      （``similarity_search_with_relevance_scores`` 抛 ``NotImplementedError``）；
    - ``relevance_fn=None``：后端不提供距离换算函数
      （``_select_relevance_score_fn`` 抛 ``NotImplementedError``）；
    - ``fail_score_search=True``：底层检索直接失败。
    """

    def __init__(
        self,
        hits: Sequence[tuple[Document, float]] = (),
        *,
        relevance_fn: CosineRelevanceFn | None = cosine_relevance,
        supports_relevance_scores: bool = True,
        fail_score_search: bool = False,
    ) -> None:
        self.hits = list(hits)
        self.relevance_fn = relevance_fn
        self.supports_relevance_scores = supports_relevance_scores
        self.fail_score_search = fail_score_search
        #: 统计底层被调用次数，用于验证空查询不会触达向量库
        self.score_search_calls = 0
        self.search_calls = 0
        #: 最近一次实际传给向量库的查询串，用于验证查询规整
        self.last_query: str | None = None

    # ------------------------------------------------------------ VectorStore 契约

    def similarity_search(
        self,
        query: str,
        k: int = 4,
        **kwargs: Any,
    ) -> list[Document]:
        self.search_calls += 1
        return [document for document, _ in self.hits[:k]]

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        self.score_search_calls += 1
        self.last_query = query
        if self.fail_score_search:
            raise RuntimeError("模拟向量库检索失败")
        return [(document, float(distance)) for document, distance in self.hits[:k]]

    def similarity_search_with_relevance_scores(
        self,
        query: str,
        k: int = 4,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        if not self.supports_relevance_scores:
            raise NotImplementedError("模拟后端不提供相关性分数")
        return super().similarity_search_with_relevance_scores(query, k=k, **kwargs)

    def _select_relevance_score_fn(self) -> CosineRelevanceFn:
        if self.relevance_fn is None:
            raise NotImplementedError("模拟后端不提供相关性换算函数")
        return self.relevance_fn

    @classmethod
    def from_texts(  # type: ignore[override]
        cls,
        texts: list[str],
        embedding: Embeddings,
        metadatas: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> FakeVectorStore:
        raise NotImplementedError("替身不支持写入，测试请直接构造 hits")


class ScriptedVectorStore(VectorStore):
    """按查询脚本返回命中的离线替身：``query -> [(文档, 距离)]``。

    ``FakeVectorStore`` 对任何查询都返回同一批 hits，无法表达「不同问题召回不同文档」；
    评估数据集里每条用例的问题都不同，因此需要按查询分派的替身。

    未登记的查询返回 ``default_hits``（默认空列表），用于模拟「该问题什么都没搜到」。
    与 ``FakeVectorStore`` 一样只实现 LangChain 抽象，且把距离交给换算函数，
    保证检索器走的是「距离 -> 相关性」路径。
    """

    def __init__(
        self,
        script: Mapping[str, Sequence[tuple[Document, float]]] | None = None,
        *,
        default_hits: Sequence[tuple[Document, float]] = (),
        relevance_fn: CosineRelevanceFn | None = cosine_relevance,
    ) -> None:
        self.script: dict[str, list[tuple[Document, float]]] = {
            query: list(hits) for query, hits in (script or {}).items()
        }
        self.default_hits = list(default_hits)
        self.relevance_fn = relevance_fn
        #: 依次收到的查询串，用于验证评估确实把问题交给了检索器
        self.queries: list[str] = []

    def similarity_search(
        self,
        query: str,
        k: int = 4,
        **kwargs: Any,
    ) -> list[Document]:
        return [document for document, _ in self._hits(query, k)]

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        **kwargs: Any,
    ) -> list[tuple[Document, float]]:
        self.queries.append(query)
        return [(document, float(distance)) for document, distance in self._hits(query, k)]

    def _select_relevance_score_fn(self) -> CosineRelevanceFn:
        if self.relevance_fn is None:
            raise NotImplementedError("模拟后端不提供相关性换算函数")
        return self.relevance_fn

    @classmethod
    def from_texts(  # type: ignore[override]
        cls,
        texts: list[str],
        embedding: Embeddings,
        metadatas: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ScriptedVectorStore:
        raise NotImplementedError("替身不支持写入，测试请直接提供查询脚本")

    # ------------------------------------------------------------------ 内部实现

    def _hits(self, query: str, k: int) -> list[tuple[Document, float]]:
        # 刻意保留脚本内的原始顺序（通常是乱序），由检索器负责换算与排序
        return self.script.get(query, self.default_hits)[:k]


class HashingEmbeddings(Embeddings):
    """确定性哈希向量：离线、可复现，用于驱动真实向量库做集成测试。

    中文按字 + 二元组切分，因此字面相近的文本向量也相近，
    足以让检索产生稳定的排序结果。
    """

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    # ------------------------------------------------------------------ 内部实现

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in self._tokenize(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % self._dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        chars = [char for char in text if not char.isspace()]
        if not chars:
            return [text]
        tokens = list(chars)
        tokens.extend("".join(pair) for pair in zip(chars, chars[1:]))
        return tokens


class CountingEmbeddings(HashingEmbeddings):
    """可观测的确定性向量：统计 ``embed_documents`` 的调用次数与文本条数。

    增量索引的核心承诺之一是「UNCHANGED 不重新 embedding」，
    仅断言向量库内容无法验证这一点，因此需要能观测到调用次数的 Embedding。
    """

    def __init__(self, dim: int = 64) -> None:
        super().__init__(dim)
        #: ``embed_documents`` 的调用次数（一次批量写入算一次）
        self.document_calls = 0
        #: 累计提交的文档条数
        self.document_texts = 0
        #: ``embed_query`` 的调用次数（检索侧）
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        self.document_texts += len(texts)
        return super().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return super().embed_query(text)

    def reset_counters(self) -> None:
        """清零计数，便于在两次索引之间比较。"""

        self.document_calls = 0
        self.document_texts = 0
        self.query_calls = 0


class ScriptedChatModel(BaseChatModel):
    """按脚本逐轮返回固定 ``AIMessage`` 的聊天模型（离线、确定性）。

    为什么需要它：Tool Calling 的核心断言是「模型产出 ``tool_calls`` 时，
    图会走 ToolNode 执行工具、再把 ``ToolMessage`` 送回模型」。
    用真实模型无法稳定复现这一步（受温度、版本、网络抖动影响），
    因此本替身让每一步输出完全可控，同时**保留 LangChain 的原生消息契约**：
    调用意图仍是 ``AIMessage.tool_calls``，工具结果仍是 ``ToolMessage``，
    没有任何自定义协议或字符串解析。

    行为：

    - ``bind_tools`` 不改变返回值，但记录被绑定的工具名，
      用于断言「模型确实看到了 get_exam_schedule」；
    - 第 n 次 ``invoke`` 返回 ``responses[n]``；脚本用尽后返回兜底回复
      （便于断言「不该被调用的轮次」）；
    - ``received`` 按调用顺序保存每次收到的完整消息列表，
      用于断言 tool result 确实回流到了模型。
    """

    responses: list[AIMessage] = Field(default_factory=list)
    #: 脚本耗尽后的回复内容
    exhausted_content: str = "（脚本已耗尽）"
    #: 每次 invoke 实际收到的消息（按调用顺序）
    received: list[list[AnyMessage]] = Field(default_factory=list)
    #: 最近一次 ``bind_tools`` 收到的工具名
    bound_tool_names: list[str] = Field(default_factory=list)

    @classmethod
    def scripted(cls, *responses: AIMessage, **kwargs: Any) -> ScriptedChatModel:
        """用一串预设回复构造模型（第 n 次调用返回第 n 条）。"""

        return cls(responses=list(responses), **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    @property
    def calls(self) -> int:
        """已发生的模型调用次数。"""

        return len(self.received)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        **kwargs: Any,
    ) -> ScriptedChatModel:
        """记录被绑定的工具名后返回自身：脚本行为不因绑定而改变。"""

        self.bound_tool_names = [
            getattr(tool, "name", None) or str(tool) for tool in tools
        ]
        return self

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        index = len(self.received) - 1
        message = (
            self.responses[index]
            if index < len(self.responses)
            else AIMessage(content=self.exhausted_content)
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class StaticIntentClassifier:
    """固定返回同一意图的识别器替身（离线、确定性）。

    图侧只依赖 ``IntentClassifier`` 协议，因此这里不需要任何模型，
    用它可以精确控制「这一轮会进哪个分支」。
    """

    def __init__(self, intent: Intent = "chat") -> None:
        self.intent: Intent = intent
        #: 依次收到的待分类文本，用于断言路由读取的是最新一条用户消息
        self.seen: list[str] = []

    def classify(self, text: str) -> Intent:
        self.seen.append(text)
        return self.intent


class ScriptedIntentClassifier:
    """按脚本**逐轮**返回意图的识别器替身（离线、确定性）。

    为什么需要它：``StaticIntentClassifier`` 对每一轮都返回同一意图，而多轮任务
    状态的用例恰恰要表达「第一轮在查成绩，第二轮用户只补一句学期」——两轮的
    孤立识别结果本来就不同（第二轮那句单独看什么都不是）。
    脚本用尽后重复返回最后一个取值，因此只需要关心会变的那几轮。
    """

    def __init__(self, *intents: Intent) -> None:
        if not intents:
            raise ValueError("ScriptedIntentClassifier 至少要给一个意图")
        self.intents: tuple[Intent, ...] = tuple(intents)
        #: 依次收到的待分类文本（长度即已执行的轮数）
        self.seen: list[str] = []

    def classify(self, text: str) -> Intent:
        index = min(len(self.seen), len(self.intents) - 1)
        self.seen.append(text)
        return self.intents[index]


class FakeRAGService:
    """RAG 服务替身：返回固定答案，不触达向量库 / Embedding / 模型。

    用于验证「knowledge 分支仍走 RAG，且与 Tool Calling 互不影响」。
    """

    def __init__(
        self,
        answer: str = "图书馆开放时间为周一至周五 8:00–22:00。",
        *,
        sources: Sequence[dict[str, Any]] = (),
        retrieved: int = 1,
    ) -> None:
        self.answer_text = answer
        self.sources = tuple(sources)
        self.retrieved = retrieved
        #: 依次收到的 (query, history 长度)，用于断言问题切分正确
        self.calls: list[tuple[str, int]] = []

    def answer(
        self, query: str, *, history: Sequence[AnyMessage] | None = None
    ) -> RAGAnswer:
        """返回预设的 RAG 答案（签名与 ``RAGService.answer`` 保持一致）。"""

        self.calls.append((query, len(list(history)) if history else 0))
        return RAGAnswer(
            query=query,
            answer=self.answer_text,
            sources=self.sources,
            retrieved=self.retrieved,
        )


class FakeExample:
    """远端 Example 的替身。

    字段名与官方 ``langsmith.schemas.Example`` 对齐（``id`` / ``dataset_id`` /
    ``inputs`` / ``outputs`` / ``metadata``），因此同步模块里读远端字段的代码
    （包括「读不到 ``case_id`` 就跳过」这类分支）走的都是与线上一致的数据形态。
    """

    def __init__(
        self,
        *,
        example_id: str,
        dataset_id: str,
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.id = example_id
        self.dataset_id = dataset_id
        self.inputs: dict[str, Any] = dict(inputs or {})
        self.outputs: dict[str, Any] = dict(outputs or {})
        self.metadata: dict[str, Any] = dict(metadata or {})


class FakeDataset:
    """远端 Dataset 的替身（官方对象同样只有一个被用到的 ``id``）。"""

    def __init__(self, *, dataset_id: str, dataset_name: str) -> None:
        self.id = dataset_id
        self.name = dataset_name


class FakeDatasetClient:
    """``langsmith.Client`` 的 Dataset 侧替身：不建连、不发请求。

    只实现同步真正用到的那六个方法（``has_dataset`` / ``create_dataset`` /
    ``read_dataset`` / ``list_examples`` / ``create_examples`` / ``update_example``），
    并记录每一次调用。这样才能断言「第二次同步一条都没写」这类幂等语义——
    只看最终条数的话，「每次重新建一遍」与「正确跳过」长得一模一样。

    刻意**不实现** ``delete_example`` 等方法：本阶段不允许删除远端条目，
    替身没有这个能力，越界调用会直接 AttributeError 暴露出来。
    """

    def __init__(self) -> None:
        #: dataset 名字 -> id
        self.datasets: dict[str, str] = {}
        #: dataset 名字 -> 创建时的 description
        self.descriptions: dict[str, str | None] = {}
        #: example id -> FakeExample
        self.examples: dict[str, FakeExample] = {}
        self.has_dataset_calls: list[str] = []
        self.read_dataset_calls: list[str] = []
        self.create_dataset_calls: list[str] = []
        self.list_examples_calls: list[str] = []
        #: 每次 ``create_examples`` 写入的条数
        self.create_examples_calls: list[int] = []
        #: 被 ``update_example`` 更新的 example id
        self.update_example_calls: list[str] = []
        self._counter = 0

    # ------------------------------------------------------------ 内部工具

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def dataset_id(self, dataset_name: str) -> str:
        """取 dataset id（不存在则抛错，便于用例暴露「以为建了其实没建」）。"""

        try:
            return self.datasets[dataset_name]
        except KeyError:  # pragma: no cover - 用例写错时立即失败
            raise AssertionError(f"替身里没有 Dataset: {dataset_name}") from None

    def examples_of(self, dataset_name: str) -> list[FakeExample]:
        """列出某个 Dataset 下当前的 Example（顺序与写入顺序一致）。"""

        dataset_id = self.dataset_id(dataset_name)
        return [item for item in self.examples.values() if item.dataset_id == dataset_id]

    def seed_example(
        self,
        dataset_name: str,
        *,
        inputs: Mapping[str, Any],
        outputs: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> FakeExample:
        """往已有 Dataset 里塞一条 Example（用于构造「远端已有数据」的初始状态）。"""

        example = FakeExample(
            example_id=self._next_id("example"),
            dataset_id=self.dataset_id(dataset_name),
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
        )
        self.examples[example.id] = example
        return example

    # ------------------------------------------------------------ 与官方客户端一致

    def has_dataset(self, *, dataset_name: str) -> bool:
        self.has_dataset_calls.append(dataset_name)
        return dataset_name in self.datasets

    def create_dataset(self, dataset_name: str, *, description: str | None = None) -> FakeDataset:
        self.create_dataset_calls.append(dataset_name)
        dataset_id = self._next_id("dataset")
        self.datasets[dataset_name] = dataset_id
        self.descriptions[dataset_name] = description
        return FakeDataset(dataset_id=dataset_id, dataset_name=dataset_name)

    def read_dataset(self, *, dataset_name: str) -> FakeDataset:
        self.read_dataset_calls.append(dataset_name)
        return FakeDataset(
            dataset_id=self.dataset_id(dataset_name), dataset_name=dataset_name
        )

    def list_examples(self, dataset_id: Any) -> list[FakeExample]:
        self.list_examples_calls.append(str(dataset_id))
        return [
            item for item in self.examples.values() if item.dataset_id == str(dataset_id)
        ]

    def create_examples(
        self, *, dataset_id: Any, examples: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        ids: list[str] = []
        for payload in examples:
            example = FakeExample(
                example_id=self._next_id("example"),
                dataset_id=str(dataset_id),
                inputs=payload.get("inputs"),
                outputs=payload.get("outputs"),
                metadata=payload.get("metadata"),
            )
            self.examples[example.id] = example
            ids.append(example.id)
        self.create_examples_calls.append(len(ids))
        return {"count": len(ids), "example_ids": ids}

    def update_example(
        self,
        example_id: Any,
        *,
        inputs: Mapping[str, Any],
        outputs: Mapping[str, Any],
        metadata: Mapping[str, Any],
        dataset_id: Any,
    ) -> dict[str, Any]:
        self.update_example_calls.append(str(example_id))
        example = self.examples[str(example_id)]
        example.dataset_id = str(dataset_id)
        example.inputs = dict(inputs)
        example.outputs = dict(outputs)
        example.metadata = dict(metadata)
        return {"id": example.id}


__all__ = [
    "CAMPUS_TEST_BASE_URL",
    "COURSE_SCHEDULE_PAYLOAD",
    "COURSE_SCHEDULE_TABLE",
    "DEFAULT_CAMPUS_PAYLOADS",
    "EXAM_SCHEDULE_PAYLOAD",
    "LEAVE_APPLICATION_PAYLOAD",
    "MCP_COURSE_TOOL_DESCRIPTION",
    "MCP_COURSE_TOOL_INPUT_SCHEMA",
    "MCP_COURSE_TOOL_NAME",
    "MCP_LEAVE_TOOL_DESCRIPTION",
    "MCP_LEAVE_TOOL_INPUT_SCHEMA",
    "MCP_LEAVE_TOOL_NAME",
    "MCP_SCORE_TOOL_DESCRIPTION",
    "MCP_SCORE_TOOL_INPUT_SCHEMA",
    "MCP_SCORE_TOOL_NAME",
    "MCP_TOOL_DESCRIPTION",
    "MCP_TOOL_INPUT_SCHEMA",
    "MCP_TOOL_NAME",
    "SCORE_PAYLOAD",
    "SCORE_TABLE",
    "TOOL_MODULES_WITH_CAMPUS_CLIENT",
    "WEEKDAY_LABELS",
    "CosineRelevanceFn",
    "CountingEmbeddings",
    "FakeDataset",
    "FakeDatasetClient",
    "FakeExample",
    "FakeMCPClient",
    "FakeRAGService",
    "FakeVectorStore",
    "HashingEmbeddings",
    "ScriptedChatModel",
    "ScriptedIntentClassifier",
    "ScriptedVectorStore",
    "StaticIntentClassifier",
    "cosine_relevance",
    "course_schedule_payload",
    "course_stub_response",
    "make_campus_api_settings",
    "make_campus_client",
    "make_document",
    "make_ingest_settings",
    "make_mcp_settings",
    "make_mcp_tool_definition",
    "make_mcp_course_tool_definition",
    "make_mcp_score_tool_definition",
    "make_mcp_server_tool_definitions",
    "make_rag_settings",
    "patch_campus_api",
    "patch_campus_api_for_tools",
    "patch_campus_api_in",
    "score_payload",
    "score_stub_response",
]
