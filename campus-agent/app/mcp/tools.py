"""把 MCP 工具适配成 LangChain 工具（供 LangGraph ``ToolNode`` 使用）。

为什么必须有这一层
------------------

LangGraph 的 ``ToolNode`` 与 ``bind_tools`` 只认 LangChain ``BaseTool``
（它们要读 ``name`` / ``description`` / ``args_schema`` / ``invoke``），
而 MCP SDK 给出的是 ``mcp.types.Tool`` + ``ClientSession.call_tool``。
两者之间需要一个转换，本模块就是那个转换，且**刻意做薄**：

- 不做业务转换、不解析业务字段、不做重试或降级；
- ``description`` 与 ``args_schema`` **来自服务端 ``tools/list`` 的发现结果**，
  不是在本模块里重写一份（否则就等于第二份工具说明书，必然与服务端漂移）；
- ``_run`` 只做两件事：合并参数 → ``MCP Client.call_tool``。

一个刻意设计的点：**被宿主绑定的参数不会暴露给模型**
----------------------------------------------------

MCP Server 上的工具（``get_exam_schedule`` / ``get_course_schedule`` /
``get_score`` / ``submit_leave_application``）都需要 ``student_id``，但
「当前学生是谁」由身份配置决定，不该由模型填
（模型既不知道也不应猜到别人的学号）。因此 ``bound_arguments`` 里的参数会：

1. 从暴露给模型的 ``args_schema`` 中**移除**——对 ``get_exam_schedule`` 来说
   模型看到的是一个无参工具，与 MCP 关闭时的直连工具**完全一致**；
2. 在每次调用时**合并进实际参数**——由宿主用配置补齐，且**优先于调用方给出的同名参数**
   （被绑定的是宿主身份，不该存在被覆盖的入口）。

于是「走 MCP」与「直连」对模型而言是同一件事，差别只在链路上多一跳。
``get_course_schedule`` 的 ``week``、``get_score`` 的 ``semester`` / ``course``
与 ``submit_leave_application`` 的 ``start_time`` / ``end_time`` / ``reason``
**不**属于绑定参数：它们是用户意图的一部分，仍由模型按措辞决定填什么
（见 ``_resolve_spec`` 对可选参数 schema 的处理）。写操作上这条边界尤其重要：
身份被绑定意味着模型无法「替别人提交请假申请」。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final, Optional

import mcp.types as mcp_types
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, create_model

from app.config.settings import (
    MCPSettings,
    get_campus_api_settings,
    get_mcp_settings,
)
from app.mcp.client import MCPClientError, StdioMCPClient, get_mcp_client

logger = logging.getLogger(__name__)

#: 经 MCP 暴露的考试工具名（与 ``app.mcp.server`` 注册的名字一致）
EXAM_TOOL_NAME: Final[str] = "get_exam_schedule"

#: 由宿主绑定、不暴露给模型的 MCP 参数名
EXAM_TOOL_BOUND_ARGUMENT: Final[str] = "student_id"

#: 经 MCP 暴露的课程工具名（与 ``app.mcp.server`` 注册的名字一致）
COURSE_TOOL_NAME: Final[str] = "get_course_schedule"

#: 课程工具里由宿主绑定的参数名。与考试工具相同：身份参数不该有覆盖入口。
COURSE_TOOL_BOUND_ARGUMENT: Final[str] = "student_id"

#: 经 MCP 暴露的成绩工具名（与 ``app.mcp.server`` 注册的名字一致）
SCORE_TOOL_NAME: Final[str] = "get_score"

#: 成绩工具里由宿主绑定的参数名。与另外两个工具相同：身份参数不该有覆盖入口。
SCORE_TOOL_BOUND_ARGUMENT: Final[str] = "student_id"

#: 经 MCP 暴露的**请假提交**工具名（与 ``app.mcp.server`` 注册的名字一致）。
#:
#: 它是本适配层里唯一一个**没有直连对应物**的工具：请假是写操作，
#: ``AGENT_TOOLS`` 只登记只读查询工具（由 ``tests/agent/test_tool_calling.py``
#: 守住），因此它不参与「按名字顶替」，而是由 :func:`build_agent_tools`
#: 在 MCP 模式下**追加**到工具清单末尾。
LEAVE_TOOL_NAME: Final[str] = "submit_leave_application"

#: 请假工具里由宿主绑定的参数名。与前三个工具同理，而且这里更要紧：
#: 写操作一旦让模型能填学号，就等于允许「替别人提交请假申请」。
LEAVE_TOOL_BOUND_ARGUMENT: Final[str] = "student_id"

#: JSON Schema 基础类型 -> Python 类型。只覆盖实际用得到的类型：
#: 遇到未登记的类型时退化为 ``Any``（不猜、也不报错）。
_JSON_SCHEMA_TYPES: Final[dict[str, Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _resolve_spec(raw_spec: Any) -> Mapping[str, Any]:
    """把一条 JSON Schema 字段定义归一化成「带 ``type`` 的那一层」。

    为什么需要它：MCP SDK 为**可选参数**生成的是 ``anyOf`` 形态——
    ``week: int | None = None`` 会变成::

        {"anyOf": [{"type": "integer", "description": "..."}, {"type": "null"}],
         "default": null}

    类型与 ``description`` 都嵌在分支里，顶层没有 ``type``。若不处理，
    适配层会把这类参数一律当成 ``Any``，并且**丢掉 description**——
    模型既不知道这个参数是什么类型，也读不到「教学周序号」这类说明，
    只能靠参数名猜，直接削弱它「该不该传这个参数」的判断。

    这里取**第一个非 null 分支**作为类型来源（``anyOf`` 里非 null 分支通常只有一个），
    并在外层缺少说明/default 时用分支的补上。
    """

    if not isinstance(raw_spec, Mapping):
        return {}
    if "type" in raw_spec or "anyOf" not in raw_spec:
        return raw_spec
    alternatives = raw_spec.get("anyOf")
    if not isinstance(alternatives, (list, tuple)):
        return raw_spec
    for alternative in alternatives:
        if isinstance(alternative, Mapping) and alternative.get("type") != "null":
            merged = dict(alternative)
            for key in ("description", "default", "title"):
                if key not in merged and key in raw_spec:
                    merged[key] = raw_spec[key]
            return merged
    return raw_spec


def build_args_schema(
    mcp_tool: mcp_types.Tool, *, bound_arguments: Mapping[str, Any] | None = None
) -> type[BaseModel]:
    """按 MCP 工具的 ``inputSchema`` 生成 LangChain 用的参数模型。

    Args:
        mcp_tool: ``tools/list`` 返回的工具定义。
        bound_arguments: 由宿主绑定的参数名集合；这些参数**不进入模型 schema**
            （值由 ``MCPToolAdapter`` 在调用时补齐）。

    Returns:
        由 ``inputSchema`` 生成的 pydantic 模型；所有参数都被绑定时是**空模型**。
    """

    schema = mcp_tool.input_schema if isinstance(mcp_tool.input_schema, Mapping) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = set(schema.get("required") or ())
    bound = set(bound_arguments or ())

    fields: dict[str, tuple[Any, Any]] = {}
    for parameter, raw_spec in properties.items():
        if parameter in bound:
            continue
        spec = _resolve_spec(raw_spec)
        annotation = _JSON_SCHEMA_TYPES.get(str(spec.get("type")), Any)
        description = spec.get("description")
        if parameter in required:
            fields[parameter] = (annotation, Field(description=description))
        else:
            fields[parameter] = (
                Optional[annotation],
                Field(default=spec.get("default"), description=description),
            )

    return create_model(f"{mcp_tool.name}_arguments", **fields)


def _as_mapping(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """把 ``BaseTool`` 传来的位置/关键字参数统一成一个参数字典。

    LangChain 调用工具时可能给出「一个字典位置参数」或「关键字参数」两种形态，
    这里都接受，避免适配层对调用方式挑食。
    """

    provided: dict[str, Any] = {}
    if args and isinstance(args[0], Mapping):
        provided.update(args[0])
    provided.update(kwargs)
    return provided


class MCPToolAdapter(BaseTool):
    """把 MCP Server 上的一个工具暴露成 LangChain ``BaseTool``。

    这是唯一的适配层：不复制业务逻辑，``_run`` 只做「合并参数 + 调 MCP Client」。
    """

    #: 由宿主绑定、每次调用自动补齐的 MCP 参数（同时不出现在 args_schema 里）
    bound_arguments: dict[str, Any] = Field(default_factory=dict)

    _client: StdioMCPClient = PrivateAttr()
    _remote_tool: mcp_types.Tool = PrivateAttr()

    @property
    def remote_tool(self) -> mcp_types.Tool:
        """远端工具定义（``tools/list`` 的原始结果），便于日志与测试核对。"""

        return self._remote_tool

    @property
    def remote_name(self) -> str:
        """远端工具名（即 MCP Server 上注册的名字）。"""

        return self._remote_tool.name

    def merged_arguments(self, provided: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """合并「调用方给出的参数」与「宿主绑定的参数」并返回实际调用参数。

        绑定参数**优先于**同名入参。被绑定的是宿主身份（如学号）：它既不在
        模型可见的 schema 里，也不该有任何覆盖入口——否则一个「对模型隐藏」
        的参数反而变成越权查询他人数据的后门。模型能填的参数一律照传。
        """

        arguments = {
            key: value for key, value in dict(provided or {}).items() if value is not None
        }
        arguments.update(self.bound_arguments)
        return arguments

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """同步调用（``ToolNode`` 走的就是这条路径）。"""

        arguments = self.merged_arguments(_as_mapping(args, kwargs))
        return self._client.call_tool(self._remote_tool.name, arguments)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """异步调用（图以 ``ainvoke`` 方式执行时使用）。"""

        arguments = self.merged_arguments(_as_mapping(args, kwargs))
        return await self._client.acall_tool(self._remote_tool.name, arguments)


def make_mcp_tool(
    client: StdioMCPClient,
    mcp_tool: mcp_types.Tool,
    *,
    bound_arguments: Mapping[str, Any] | None = None,
) -> MCPToolAdapter:
    """由「已发现的 MCP 工具定义」构造适配器。

    ``name`` / ``description`` / ``args_schema`` 全部取自 ``mcp_tool``，
    因此「模型看到的工具」与「服务端注册的工具」不会各说一套。

    私有属性（客户端与远端定义）在构造后显式赋值：它们是运行期依赖，
    不参与 pydantic 校验，也不应出现在工具的序列化输出里。
    """

    bound = dict(bound_arguments or {})
    adapter = MCPToolAdapter(
        name=mcp_tool.name,
        description=(mcp_tool.description or "").strip(),
        args_schema=build_args_schema(mcp_tool, bound_arguments=bound),
        bound_arguments=bound,
    )
    adapter._client = client
    adapter._remote_tool = mcp_tool
    return adapter


def _build_mcp_tool(
    client: StdioMCPClient,
    tool_name: str,
    *,
    label: str,
    bound_argument: str,
    settings: MCPSettings | None = None,
) -> MCPToolAdapter:
    """连上 MCP Server、按名发现工具，并生成「身份已绑定」的 LangChain 工具。

    两个工具（考试 / 课程）的装配步骤完全相同，只有名字与日志标签不同，
    因此收敛到这一处：差异只以参数出现，避免两段近乎一样的代码各自演化。

    Raises:
        MCPClientError: 服务不可用或未提供该工具。
    """

    mcp_settings = settings or get_mcp_settings()
    client.start()  # 建连 + initialize + 工具发现（幂等，重复调用不会重开进程）
    mcp_tool = client.find_tool(tool_name)
    student_id = get_campus_api_settings().default_student_id
    logger.info(
        "%s工具改由 MCP 提供: server=%s tool=%s input_schema=%s",
        label,
        mcp_settings.server_name,
        mcp_tool.name,
        dict(mcp_tool.input_schema or {}),
    )
    return make_mcp_tool(
        client,
        mcp_tool,
        bound_arguments={bound_argument: student_id},
    )


def build_exam_mcp_tool(
    client: StdioMCPClient, *, settings: MCPSettings | None = None
) -> MCPToolAdapter:
    """连上 MCP Server 并为其 ``get_exam_schedule`` 生成 LangChain 工具。

    Raises:
        MCPClientError: 服务不可用或未提供该工具。
    """

    return _build_mcp_tool(
        client,
        EXAM_TOOL_NAME,
        label="考试",
        bound_argument=EXAM_TOOL_BOUND_ARGUMENT,
        settings=settings,
    )


def build_course_mcp_tool(
    client: StdioMCPClient, *, settings: MCPSettings | None = None
) -> MCPToolAdapter:
    """连上 MCP Server 并为其 ``get_course_schedule`` 生成 LangChain 工具。

    与考试工具**共用同一个 Server 会话**（``start()`` 幂等，不会拉起第二个子进程）。
    ``student_id`` 同样由宿主绑定、不进模型可见的 schema；``week`` 保持可选，
    仍由模型按用户措辞决定是否填写。

    Raises:
        MCPClientError: 服务不可用或未提供该工具。
    """

    return _build_mcp_tool(
        client,
        COURSE_TOOL_NAME,
        label="课程",
        bound_argument=COURSE_TOOL_BOUND_ARGUMENT,
        settings=settings,
    )


def build_score_mcp_tool(
    client: StdioMCPClient, *, settings: MCPSettings | None = None
) -> MCPToolAdapter:
    """连上 MCP Server 并为其 ``get_score`` 生成 LangChain 工具。

    与考试 / 课程工具**共用同一个 Server 会话**（``start()`` 幂等，不会拉起第三个
    子进程）。``student_id`` 同样由宿主绑定、不进模型可见的 schema；
    ``semester`` 与 ``course`` 保持可选，仍由模型按用户措辞决定填不填。

    Raises:
        MCPClientError: 服务不可用或未提供该工具。
    """

    return _build_mcp_tool(
        client,
        SCORE_TOOL_NAME,
        label="成绩",
        bound_argument=SCORE_TOOL_BOUND_ARGUMENT,
        settings=settings,
    )


def build_leave_mcp_tool(
    client: StdioMCPClient, *, settings: MCPSettings | None = None
) -> MCPToolAdapter:
    """连上 MCP Server 并为其 ``submit_leave_application`` 生成 LangChain 工具。

    与另外三个工具**共用同一个 Server 会话**（``start()`` 幂等，不会为写操作另起
    进程）。``student_id`` 由宿主绑定、不进模型可见的 schema——模型能填的只有
    ``start_time`` / ``end_time`` / ``reason`` 三项，也就是「请假这件事本身」。

    ``start_time`` / ``end_time`` / ``reason`` 都是**必填**：它们会生成
    必填的 ``args_schema`` 字段，模型在看到用户信息不全时会先追问，
    而不是提交一条缺字段的申请（服务端也会以 400 拒绝）。

    Raises:
        MCPClientError: 服务不可用或未提供该工具。
    """

    return _build_mcp_tool(
        client,
        LEAVE_TOOL_NAME,
        label="请假提交",
        bound_argument=LEAVE_TOOL_BOUND_ARGUMENT,
        settings=settings,
    )


#: 经 MCP 暴露的全部工具：(MCP 工具名, 适配器构建函数)。
#:
#: 多数条目的名字与 ``AGENT_TOOLS`` 里的直连工具**同名**，因此替换时「按名字对位」
#: 即可，不需要额外的映射表；请假提交没有同名直连工具，由 ``build_agent_tools``
#: 追加（见 ``LEAVE_TOOL_NAME`` 的说明）。
_MCP_TOOL_BUILDERS: Final[tuple[tuple[str, Callable[..., MCPToolAdapter]], ...]] = (
    (EXAM_TOOL_NAME, build_exam_mcp_tool),
    (COURSE_TOOL_NAME, build_course_mcp_tool),
    (SCORE_TOOL_NAME, build_score_mcp_tool),
    (LEAVE_TOOL_NAME, build_leave_mcp_tool),
)


def build_agent_tools(
    *, settings: MCPSettings | None = None, client: StdioMCPClient | None = None
) -> tuple[BaseTool, ...]:
    """按配置决定**哪些工具经 MCP 提供**，返回 Agent 本轮要用的工具清单。

    两类工具的处理方式不同，这是刻意的：

    =======================  =====================  ==========================
    能力                     实现                   进入清单的方式
    =======================  =====================  ==========================
    ``get_exam_schedule``    MCP / 直连（按配置）    按名字**顶替**同名的直连工具
    ``get_course_schedule``  MCP / 直连（按配置）    同上
    ``get_score``            MCP / 直连（按配置）    同上
    ``submit_leave_application``  **仅** MCP        **追加**到清单末尾
    =======================  =====================  ==========================

    - **顶替**：查询能力各只有**一个槽位**。要么是 MCP 适配器，要么是直连工具，
      不存在「两套同时注册」（那会让模型看到两个同名能力，行为不可预测）。
    - **追加**：请假提交是写操作，``AGENT_TOOLS`` 只登记只读查询工具
      （有守卫用例），因此它不参与顶替——MCP 提供了就追加，没提供就不给模型
      一个必然失败的工具（只记 WARNING）。这也意味着
      ``CAMPUS_MCP_ENABLED=false`` 时请假提交能力不可用，而三个查询照常直连。

    **回退粒度是「按工具」而不是「全有或全无」**：MCP Server 拉不起来时全部槽位
    都无法顶替，结果等价于「整体回退为直连」（且请假能力缺席）；若只是某个工具在
    服务端缺失（例如两侧版本不一致），则只有那一个槽位回退，其余仍走 MCP——
    「MCP 少注册一个工具」不该把已经可用的能力一起降级。
    回退一律以 WARNING 明确记录，避免静默地以为在走 MCP。

    Args:
        settings: 注入的 MCP 配置；``None`` 时按环境变量解析。
        client: 注入的 MCP 客户端（测试用替身）；``None`` 时用进程内单例。
    """

    # 延迟导入：本模块属于基础设施层，不应在导入期就把 agent 层（含图与 RAG）
    # 拖进来；同时避免 app.mcp 与 app.agent 之间形成导入顺序上的耦合。
    from app.agent.nodes.tool_agent import AGENT_TOOLS

    mcp_settings = settings or get_mcp_settings()
    if not mcp_settings.enabled:
        logger.info(
            "MCP 未启用（CAMPUS_MCP_ENABLED=false），考试、课程与成绩查询直连校园"
            "业务服务；请假提交只在 MCP 上暴露，因此该模式下不可用"
        )
        return tuple(AGENT_TOOLS)

    mcp_client = client or get_mcp_client()
    replacements: dict[str, BaseTool] = {}
    for tool_name, build in _MCP_TOOL_BUILDERS:
        try:
            adapter = build(mcp_client, settings=mcp_settings)
        except MCPClientError as exc:
            logger.warning(
                "MCP 未提供 %s，该能力回退为直连校园业务服务: %s", tool_name, exc
            )
            continue
        replacements[adapter.name] = adapter

    tools: list[BaseTool] = [
        replacements.get(tool.name, tool) for tool in AGENT_TOOLS
    ]
    leave_tool = replacements.get(LEAVE_TOOL_NAME)
    if leave_tool is not None:
        tools.append(leave_tool)
    return tuple(tools)


__all__ = [
    "COURSE_TOOL_BOUND_ARGUMENT",
    "COURSE_TOOL_NAME",
    "EXAM_TOOL_BOUND_ARGUMENT",
    "EXAM_TOOL_NAME",
    "LEAVE_TOOL_BOUND_ARGUMENT",
    "LEAVE_TOOL_NAME",
    "MCPToolAdapter",
    "SCORE_TOOL_BOUND_ARGUMENT",
    "SCORE_TOOL_NAME",
    "build_agent_tools",
    "build_args_schema",
    "build_course_mcp_tool",
    "build_exam_mcp_tool",
    "build_leave_mcp_tool",
    "build_score_mcp_tool",
    "make_mcp_tool",
]
