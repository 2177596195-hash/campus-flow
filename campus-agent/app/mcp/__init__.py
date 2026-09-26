"""Campus MCP：把校园业务能力以 MCP（Model Context Protocol）工具的形式对外提供。

分层与依赖方向（禁止反向依赖）::

    app.mcp.server   MCP Server：MCP 参数 -> 调 CampusApiClient -> 转 MCP 返回结果
    app.mcp.client   MCP Client：stdio 连接、工具发现（list_tools）、工具调用（call_tool）
    app.mcp.tools    LangChain 适配：把发现的 MCP 工具包成 LangChain BaseTool，供 ToolNode 使用

硬性约束：

1. MCP Server **不连接 MySQL、不写 SQL**——它通过 ``CampusApiClient`` 调用
   既有的 Spring Boot REST API，数据库始终只由 Spring Boot 访问；
2. MCP Server **不复制任何 HTTP 逻辑**（URL 拼接 / 超时 / 状态码 / JSON 解析），
   一律复用 ``app.infrastructure.campus_api``；
3. 本层的存在不是为了「有个 MCP」，而是**真正参与 Agent 的工具调用**：
   ``CAMPUS_MCP_ENABLED=true`` 时，Agent 的考试查询走
   Agent -> MCP Client -> Campus MCP Server -> CampusApiClient -> Spring Boot -> MySQL。

注意：导入本包不产生副作用（不读配置、不建连接、不启动子进程）。
实际建连发生在 ``app.mcp.client.get_mcp_client().start()`` 被首次调用的时刻。
"""

from app.mcp.client import (
    MCPClientError,
    MCPServerUnavailable,
    MCPToolInvocationError,
    MCPToolNotFound,
    MCPToolResultError,
    StdioMCPClient,
    get_mcp_client,
    reset_mcp_client,
)

__all__ = [
    "MCPClientError",
    "MCPServerUnavailable",
    "MCPToolInvocationError",
    "MCPToolNotFound",
    "MCPToolResultError",
    "StdioMCPClient",
    "get_mcp_client",
    "reset_mcp_client",
]
