"""Agent HTTP 入口（FastAPI）。

设计约束
--------

1. **最小入口**：只暴露一个 ``POST /api/agent/chat``，供前端（经 Spring Boot
   转发或 Vite 代理）发起一轮对话。不实现认证、限流、CORS——那些是网关职责，
   本阶段由 Spring Boot BFF 或 Vite 开发代理承担。

2. **复用 CLI 的全部运行时**：``_build_runtime`` / ``_invoke`` / ``_close_runtime``
   全部来自 ``main.py``，本模块不重复装配逻辑。CLI 与 HTTP 两条入口共享同一套
   图拓扑、工具、检查点后端，行为一致。

3. **多轮会话靠 ``threadId``**：前端每轮传同一个 ``threadId``，checkpointer
   据此加载历史状态。``threadId`` 缺失时自动生成一个（单轮场景），
   响应里回传本次使用的 ``threadId``，前端据此在下一轮带回。

4. **启动方式**：``python -m app.api.server``（见本文件末尾的 ``__main__`` 块）。
   默认监听 8000 端口，可用 ``AGENT_PORT`` 环境变量覆盖。

调用链
------

Vue → Spring Boot BFF → 本服务 POST /api/agent/chat → LangGraph
→ intent_router → task_slot → agent（Tool Calling）→ get_score tool
→ CampusApiClient → Spring Boot /api/scores → MySQL
→ Agent 生成自然语言 → 原路返回 Vue
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from app.agent.run_context import ENTRYPOINT_HTTP
from app.config.settings import ConfigurationError, get_settings

logger = logging.getLogger(__name__)

#: 默认监听端口。Spring Boot 占 8080、Vite 占 5173，Agent 用 8000 互不冲突。
_DEFAULT_AGENT_PORT: int = 8000

#: 运行时在模块级别持有（lifespan 写入），路由处理函数读取。
#: FastAPI 的 lifespan 保证「请求到达时 runtime 已就绪」，
#: 因此不需要加锁——startup 在第一个请求之前就跑完了。
_runtime: CompiledStateGraph | None = None


def _configured_port() -> int:
    """从环境变量读取监听端口；非法值回落到默认。"""

    raw = os.getenv("AGENT_PORT", "").strip()
    if not raw:
        return _DEFAULT_AGENT_PORT
    try:
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    logger.warning("AGENT_PORT=%r 非法，使用默认端口 %d", raw, _DEFAULT_AGENT_PORT)
    return _DEFAULT_AGENT_PORT


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """前端发来的单轮对话请求。

    ``threadId`` 可选：缺失时自动生成（单轮场景），前端从响应里取回并在下一轮带回，
    即可实现多轮。这一设计与 LangGraph checkpointer 的 ``thread_id`` 一一对应。
    """

    message: str = Field(..., min_length=1, description="用户本轮输入")
    threadId: str | None = Field(default=None, description="会话标识；缺失时自动生成")


class ChatResponse(BaseModel):
    """单轮对话的响应。

    ``reply`` 是 Agent 最终的自然语言回答（已经过工具调用、模型总结）；
    ``intent`` 是本轮识别出的意图（chat / knowledge / query / action / unknown）；
    ``toolCalls`` 是本轮调用的工具名列表（空列表表示没走工具分支）；
    ``threadId`` 是本次使用的会话标识，前端下一轮原样带回即可。
    """

    reply: str
    intent: str
    toolCalls: list[str]
    threadId: str


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    error: str
    detail: str = ""


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程级生命周期：启动时装配运行时，关闭时释放资源。

    与 ``main.py`` 的 CLI 入口一致：装配前先把**追踪配置**落地（早于任何模型 /
    工具构建），因此 HTTP 入口的请求同样会产生 trace。

    落地动作走 ``main._apply_tracing()`` 而不是自己去碰配置层：``app/`` 下的代码
    不许出现追踪相关的名字（架构守卫会把 ``app/**`` 里的出现判成「第二套追踪机制」），
    所以进程内**唯一写入点**留在 ``main.py``，两条入口共用它。
    """

    global _runtime

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.error("Agent HTTP 启动失败：配置错误 — %s", exc)
        raise

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 延迟导入：避免模块加载时就拉起 main.py 的全部依赖链
    from main import _apply_tracing, _build_runtime, _close_runtime

    # 追踪配置由 main.py 落地（进程内唯一写入点），HTTP 入口与 CLI 入口共用同一处
    _apply_tracing()

    logger.info(
        "Agent HTTP 启动 | provider=%s model=%s checkpointer=%s",
        settings.llm.provider,
        settings.llm.model,
        settings.checkpointer_backend,
    )
    _runtime = _build_runtime(settings)

    try:
        yield
    finally:
        _close_runtime(_runtime)
        _runtime = None
        logger.info("Agent HTTP 已关闭")


app = FastAPI(
    title="CampusFlow Agent",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.post("/api/agent/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """处理一轮对话。

    核心逻辑全部委托给 ``main._invoke``——它已经处理了意图识别、槽位填充、
    工具调用、模型总结等全部流程。本路由只做三件事：

    1. 校验运行时已就绪；
    2. 补齐 ``threadId``（缺失时自动生成）；
    3. 把 ``TurnResult`` 翻译成 HTTP 响应。
    """

    if _runtime is None:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(error="service_unavailable", detail="Agent 运行时未就绪").model_dump(),
        )

    thread_id = request.threadId or uuid.uuid4().hex

    from main import TurnResult, _invoke

    try:
        result: TurnResult = _invoke(
            _runtime,
            request.message,
            thread_id,
            entrypoint=ENTRYPOINT_HTTP,
        )
    except Exception:
        logger.exception("Agent HTTP 处理失败: thread=%s", thread_id)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="internal_error",
                detail="Agent 处理请求时发生内部错误，请稍后重试。",
            ).model_dump(),
        )

    return ChatResponse(
        reply=result.reply,
        intent=result.intent,
        toolCalls=list(result.tool_calls),
        threadId=thread_id,
    )


@app.get("/api/agent/health")
async def health() -> dict[str, Any]:
    """健康检查：前端 / BFF 据此判断 Agent 服务是否可用。"""

    return {"status": "ok" if _runtime is not None else "starting"}


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def main() -> None:
    """以 uvicorn 启动 Agent HTTP 服务。"""

    import uvicorn

    port = _configured_port()
    logger.info("Agent HTTP 监听端口: %d", port)
    uvicorn.run(
        "app.api.server:app",
        host="127.0.0.1",
        port=port,
        # 开发阶段开 reload；生产部署时由运维用 ``uvicorn app.api.server:app`` 覆盖
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
