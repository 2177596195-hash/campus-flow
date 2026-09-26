"""MCP Client（stdio transport）。

它解决的核心问题：**同步与异步的边界**
--------------------------------------

MCP SDK 全异步（``ClientSession.initialize`` / ``list_tools`` / ``call_tool``
都是协程，底层是 anyio），而 LangGraph 的 ``ToolNode`` 走的是**同步** ``invoke``。
本模块用「一个后台线程承载一条专属事件循环」来消化这个差异：

- 后台线程里 ``new_event_loop`` 跑一个常驻协程 ``_session_main``，
  它建立 stdio 连接、``initialize``、``list_tools``（工具发现），然后挂起等待关闭；
- 主线程侧对外只暴露**同步**方法：``start`` / ``list_tools`` / ``call_tool``，
  内部用 ``asyncio.run_coroutine_threadsafe`` 把协程投递到那条循环上。

选「常驻会话」而不是「每次调用起一个 Server 进程」的原因：MCP 的价值之一是
**会话级的能力协商与工具发现**（``initialize`` + ``tools/list`` 只做一次）。
每次调用都重开进程会让「发现」退化成摆设，也白白付出进程启动开销。
常驻会话同时保证工具调用是「同一个 Server 进程」在服务，与真实 MCP 部署一致。

生命周期：``start()`` 幂等；``close()`` 置位停止事件并等线程收尾（子进程随之退出）。
``get_mcp_client()`` 提供进程内单例，``reset_mcp_client()`` 用于改配置后重建。

错误分类（消息均为可读中文，不含命令行与 traceback）::

    MCPClientError            基类
    ├── MCPServerUnavailable  启动失败 / 连接断开 / 调用超时
    ├── MCPToolNotFound       服务端没有这个工具
    ├── MCPToolInvocationError 服务端明确返回 isError
    └── MCPToolResultError    结果既无文本也无结构化内容，无法解析
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.config.settings import MCPSettings, get_mcp_settings

logger = logging.getLogger(__name__)

#: 启动（建连 + initialize + list_tools）超时（秒）。首次启动要拉起一个
#: Python 子进程并完成协议握手，给得比单次调用宽裕。
DEFAULT_STARTUP_TIMEOUT: Final[float] = 30.0

#: 单次 MCP 工具调用超时（秒）。
DEFAULT_CALL_TIMEOUT: Final[float] = 20.0


class MCPClientError(RuntimeError):
    """MCP 客户端失败的统一基类。

    与 ``CampusApiError`` 家族同理：``str(exc)`` 是可直接转述给用户的中文描述，
    会被 ``handle_tool_error`` 原样写进 ``ToolMessage``。
    """


class MCPServerUnavailable(MCPClientError):
    """MCP Server 拉不起来、连接断开或调用超时。"""


class MCPToolNotFound(MCPClientError):
    """MCP Server 未注册该工具（工具发现的结果里没有它）。"""


class MCPToolInvocationError(MCPClientError):
    """MCP Server 明确返回了 ``isError`` 结果（业务侧失败）。"""


class MCPToolResultError(MCPClientError):
    """工具结果既无文本也无结构化内容，无法取到业务负载。"""


def _text_payload(result: mcp_types.CallToolResult) -> str | None:
    """取出结果里的文本内容（多个 TextContent 拼接）。"""

    contents = getattr(result, "content", None) or ()
    texts: list[str] = []
    for item in contents:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            texts.append(text)
    if not texts:
        return None
    return "".join(texts).strip()


#: MCP SDK 在工具失败时统一加的前缀（完整形态为 ``Error executing tool <name>``）
_SDK_ERROR_PREFIX: Final[str] = "Error executing tool "

#: 崩溃（未预期的异常）在 Server 端只会留下这句通用文案
TOOL_FAILURE_PREFIX: Final[str] = "校园 MCP 服务执行工具失败"
GENERIC_TOOL_FAILURE: Final[str] = f"{TOOL_FAILURE_PREFIX}，请稍后再试。"


def _tool_error_message(tool: mcp_types.Tool | None, result: mcp_types.CallToolResult) -> str:
    """把 ``isError`` 结果整理成可读的中文句子。

    为什么需要剥前缀：MCP SDK（``mcp/server/mcpserver/tools/base.py``）对
    **可预期的**失败（工具抛 ``ToolError``）会保留原始消息，但统一加上
    ``Error executing tool <name>: ``；对**崩溃**则只给
    ``Error executing tool <name>``（原始异常只留在 Server 端日志里，按设计
    不泄漏给客户端）。这里剥掉该前缀，让业务原因直接成为 Agent 能转述给用户的
    句子；剥完为空即说明是崩溃，退回一句通用中文。
    """

    raw = (_text_payload(result) or "").strip()
    name = getattr(tool, "name", "") or ""
    prefix = f"{_SDK_ERROR_PREFIX}{name}"
    if name and raw.startswith(prefix):
        raw = raw[len(prefix) :].lstrip(":：").strip()
    if not raw:
        return GENERIC_TOOL_FAILURE
    return f"{TOOL_FAILURE_PREFIX}: {raw}"


def _advertises_wrapped_result(tool: mcp_types.Tool | None) -> bool:
    """该工具的 ``outputSchema`` 是否只有一个 ``result`` 属性。

    MCP Python SDK 对**泛型返回值**（``list`` / ``dict`` / ``Union`` 等）
    会包一层 ``{"result": <原值>}`` 再放进 ``structuredContent``——因为
    ``structuredContent`` 按规范必须是对象，而数组不是。该包装行为由 SDK
    在发布 ``outputSchema`` 时同步声明，因此这里以「工具自己声明的输出结构」
    为准来判断，而不是把 ``"result"`` 当魔法字符串硬编码。
    """

    schema = getattr(tool, "output_schema", None)
    if not isinstance(schema, Mapping):
        return False
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return False
    return list(properties) == ["result"]


def _unwrap_structured(tool: mcp_types.Tool | None, structured: Any) -> Any:
    """拆掉 SDK 对非对象返回值加的外层包装（仅在确实匹配时才拆）。"""

    if not isinstance(structured, Mapping) or set(structured) != {"result"}:
        return structured
    if not _advertises_wrapped_result(tool):
        return structured
    return structured["result"]


def extract_tool_payload(
    result: mcp_types.CallToolResult, *, tool: mcp_types.Tool | None = None
) -> Any:
    """从 MCP 调用结果中取出业务负载。

    取值优先级是**结构化内容优先、文本内容兜底**。这个顺序来自实测而不是推测：

    - 工具返回数组时，SDK 会把 ``content`` 拆成**每个元素一个 TextContent**，
      将它们拼接得到的字符串是 ``{...}{...}``——**不是合法 JSON**，
      ``json.loads`` 必然失败。因此「文本优先」对数组返回是错的；
    - ``structuredContent`` 才是数组的忠实表示（外层包一层 ``{"result": [...]}``，
      见 ``_advertises_wrapped_result``），拆掉包装后与工具返回值逐字相等。

    仅当结构化内容缺失（例如工具只返回文本）时才退回解析文本内容。

    Args:
        result: MCP 调用结果。
        tool: 该结果对应的工具定义，用于判断是否需要拆包装；
            为 ``None`` 时不拆，直接返回 ``structuredContent``。

    Raises:
        MCPToolInvocationError: 结果为 ``isError``。
        MCPToolResultError: 既无结构化内容也无文本内容。
    """

    if bool(getattr(result, "is_error", False)):
        raise MCPToolInvocationError(_tool_error_message(tool, result))

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return _unwrap_structured(tool, structured)

    text = _text_payload(result)
    if text is None:
        raise MCPToolResultError("校园 MCP 服务返回了无法解析的结果，请联系管理员。")
    try:
        return json.loads(text)
    except ValueError:
        # MCP 工具本就允许返回纯文本；此处不作失败处理。
        return text


class StdioMCPClient:
    """以 stdio transport 拉起并持有一个 MCP Server 的同步客户端。

    Args:
        command: 启动 MCP Server 的可执行文件（默认取配置，通常为当前解释器）。
        args: 传给 command 的参数（默认为 ``-m app.mcp.server``）。
        env: 子进程环境变量；``None`` 时继承当前进程环境。
        cwd: 子进程工作目录（``None`` 时取配置，通常为 campus-agent 根目录）。
        startup_timeout: 启动超时（秒）。
        call_timeout: 单次调用超时（秒）。
        server_name: 仅用于日志与异常文案的标识。

    该类**不在构造时建连**：``start()`` 才真正拉起子进程。
    这样 Agent 图构建阶段（乃至模块导入）都不会有副作用。
    """

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        server_name: str = "campus-mcp-server",
    ) -> None:
        self._command = command
        self._args = tuple(args)
        self._env: dict[str, str] = dict(env) if env is not None else dict(os.environ)
        self._cwd = str(cwd) if cwd is not None else None
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._server_name = server_name

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._stopped: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._tools: tuple[mcp_types.Tool, ...] = ()

    # ------------------------------------------------------------------ 属性

    @property
    def server_name(self) -> str:
        """用于日志与异常文案的服务标识。"""

        return self._server_name

    @property
    def started(self) -> bool:
        """会话是否已建立（``initialize`` 与工具发现均已完成）。"""

        return self._session is not None and self._error is None

    @property
    def command_line(self) -> tuple[str, ...]:
        """启动命令（仅用于日志；不进异常消息）。"""

        return (self._command, *self._args)

    # ------------------------------------------------------------------ 生命周期

    def start(self, timeout: float | None = None) -> StdioMCPClient:
        """建立 stdio 会话并完成工具发现；重复调用是幂等的。

        Raises:
            MCPServerUnavailable: 子进程拉不起来、握手超时或握手报文异常。
        """

        with self._lock:
            if self.started:
                return self
            self._error = None
            self._ready = threading.Event()
            self._tools = ()
            self._thread = threading.Thread(
                target=self._thread_main,
                name=f"mcp-client-{self._server_name}",
                daemon=True,
            )
            self._thread.start()
            thread = self._thread

        if not self._ready.wait(self._startup_timeout if timeout is None else timeout):
            raise MCPServerUnavailable(
                f"校园 MCP 服务启动超时（{self._startup_timeout:g}s），请稍后再试。"
            )
        if self._error is not None:
            logger.warning(
                "校园 MCP 服务启动失败: %s", self._error, exc_info=self._error
            )
            raise MCPServerUnavailable("校园 MCP 服务暂时不可用，请稍后再试。") from self._error
        if thread.is_alive() is False:
            raise MCPServerUnavailable("校园 MCP 服务启动后立即退出，请稍后再试。")
        return self

    def close(self) -> None:
        """置位停止事件并等待后台线程收尾（MCP Server 子进程随之退出）。"""

        loop, stopped, thread = self._loop, self._stopped, self._thread
        if loop is not None and stopped is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stopped.set)
            except RuntimeError:  # 循环已关闭，无需再通知
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._startup_timeout)
        self._thread = None
        self._session = None

    def __enter__(self) -> StdioMCPClient:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 后台循环

    def _thread_main(self) -> None:
        """后台线程入口：建一条专属事件循环并常驻运行会话协程。"""

        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._session_main())
        except BaseException as exc:  # noqa: BLE001 - 任何异常都要让 start() 感知
            self._error = exc
            self._ready.set()
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _session_main(self) -> None:
        """常驻协程：连接 -> initialize -> 工具发现 -> 等待关闭。"""

        parameters = StdioServerParameters(
            command=self._command,
            args=list(self._args),
            env=self._env,
            cwd=self._cwd,
        )
        try:
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self._tools = tuple(listing.tools or ())
                    self._session = session
                    self._stopped = asyncio.Event()
                    logger.info(
                        "校园 MCP 会话已建立: server=%s tools=%s",
                        self._server_name,
                        ", ".join(tool.name for tool in self._tools) or "(无)",
                    )
                    self._ready.set()
                    await self._stopped.wait()
        except BaseException as exc:  # noqa: BLE001 - 交由 start()/call_tool 报告
            self._error = exc
            self._ready.set()
            raise

    def _submit(self, coroutine: Any, *, action: str) -> Any:
        """把协程投递到后台循环并同步等待结果。"""

        self.start()
        loop = self._loop
        if loop is None or loop.is_closed():
            raise MCPServerUnavailable("校园 MCP 服务连接已关闭，请稍后再试。")

        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(self._call_timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MCPServerUnavailable(
                f"校园 MCP 服务{action}超时（{self._call_timeout:g}s），请稍后再试。"
            ) from exc
        except MCPClientError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 传输层异常统一归类
            logger.warning("校园 MCP %s失败: %s", action, exc, exc_info=exc)
            raise MCPServerUnavailable(
                f"校园 MCP 服务{action}失败，请稍后再试。"
            ) from exc

    # ------------------------------------------------------------------ 工具发现

    def list_tools(self) -> tuple[mcp_types.Tool, ...]:
        """返回服务端注册的工具列表（来自 ``tools/list``）。"""

        self.start()
        return self._tools

    def find_tool(self, name: str) -> mcp_types.Tool:
        """按名字取工具定义（含 ``description`` 与 ``inputSchema``）。

        Raises:
            MCPToolNotFound: 服务端未注册该工具。
        """

        for tool in self.list_tools():
            if tool.name == name:
                return tool
        available = ", ".join(tool.name for tool in self._tools) or "(无)"
        raise MCPToolNotFound(
            f"校园 MCP 服务未提供工具 {name}，当前可用: {available}"
        )

    # ------------------------------------------------------------------ 工具调用

    async def acall_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """异步调用工具（供异步调用方使用）。"""

        self.start()
        return await self._call_tool(name, arguments)

    async def _call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        session = self._session
        if session is None:
            raise MCPServerUnavailable("校园 MCP 服务会话尚未建立，请稍后再试。")
        if self._error is not None:
            raise MCPServerUnavailable("校园 MCP 服务连接已断开，请稍后再试。") from self._error

        tool = self.find_tool(name)
        try:
            result = await session.call_tool(name, dict(arguments or {}))
        except MCPClientError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 传输层异常统一归类
            logger.warning("校园 MCP 工具 %s 调用失败: %s", name, exc, exc_info=exc)
            raise MCPServerUnavailable("校园 MCP 服务调用失败，请稍后再试。") from exc

        return extract_tool_payload(result, tool=tool)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """同步调用工具：Agent（ToolNode）走的就是这条路径。"""

        return self._submit(self._call_tool(name, arguments), action="工具调用")


@lru_cache(maxsize=1)
def get_mcp_client() -> StdioMCPClient:
    """返回进程内共享的 MCP 客户端（按配置装配，**尚未建连**）。

    与 ``get_campus_api_client()`` 同样是惰性策略：本函数只读配置，
    真正的 ``start()``（拉子进程 + 握手 + 工具发现）由首次使用触发。

    ``@lru_cache(maxsize=1)`` 实现进程内单例：重复调用（例如装配工具清单时
    多个 ``build_*_mcp_tool`` 各取一次客户端）拿到的是**同一个**实例，
    不会各自拉起一条 stdio 会话。改配置后需要重建时调用
    :func:`reset_mcp_client`（它会关闭旧实例并清空缓存）。

    配置非法（``ConfigurationError``）不会被缓存：修正环境变量后的下一次
    调用会重新装配，而不是永远拿到失败。

    Raises:
        ConfigurationError: 启动命令 / 超时等配置非法。
    """

    settings: MCPSettings = get_mcp_settings()
    logger.info(
        "校园 MCP 客户端已装配: server=%s command=%s args=%s cwd=%s",
        settings.server_name,
        settings.command,
        " ".join(settings.args) or "(无)",
        settings.cwd or "(继承)",
    )
    return StdioMCPClient(
        settings.command,
        settings.args,
        cwd=settings.cwd,
        startup_timeout=settings.startup_timeout,
        call_timeout=settings.call_timeout,
        server_name=settings.server_name,
    )


def reset_mcp_client() -> None:
    """关闭并丢弃缓存的 MCP 客户端，使下次调用按最新配置重新装配。"""

    if get_mcp_client.cache_info().currsize:
        get_mcp_client().close()
    get_mcp_client.cache_clear()


__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_STARTUP_TIMEOUT",
    "GENERIC_TOOL_FAILURE",
    "MCPClientError",
    "MCPServerUnavailable",
    "MCPToolInvocationError",
    "MCPToolNotFound",
    "MCPToolResultError",
    "TOOL_FAILURE_PREFIX",
    "StdioMCPClient",
    "extract_tool_payload",
    "get_mcp_client",
    "reset_mcp_client",
]
