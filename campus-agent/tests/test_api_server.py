"""Agent HTTP 入口（``app/api/server.py``）的基础契约测试（完全离线）。

本文件只验证「入口这一层」的职责，不验证 Agent 本身：

===================================  ==========================================
关注点                               本文件怎么测
===================================  ==========================================
健康检查                              就绪 / 未就绪两种状态的取值
请求 / 响应契约                       字段名与类型（前端 `@/api/agent` 逐字依赖）
threadId 的生成与回传                 缺失时自动生成、给了就原样沿用
参数校验                              空 message 由 Pydantic 挡在入口
运行时未就绪                          503 + ``service_unavailable``
运行时内部异常                        500 + ``internal_error``，且**不泄露内部细节**
追踪入口标记                          ``_invoke(..., entrypoint=ENTRYPOINT_HTTP)``
===================================  ==========================================

刻意**不启动 lifespan**：那会真的去装配图、连模型、拉 MCP 子进程。
因此这里把 ``server._runtime`` 换成替身，并把 ``main._build_runtime`` 换成一个
会立刻失败的函数——万一将来 TestClient 的行为变了（比如改成默认执行 lifespan），
本文件会**大声失败**，而不是悄悄去连真实服务。
``main._invoke`` 同样被替换：被测的是入口的参数搬运与异常映射，不是图。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main as cli
from app.agent.run_context import ENTRYPOINT_HTTP
from app.api import server

#: 内部异常里带的「内部细节」：只允许进日志，不允许出现在响应体里
LEAKY_DETAIL = "SQLITE_IOERR: /var/lib/secret/checkpoints.sqlite 写入失败"


def _client() -> TestClient:
    return TestClient(server.app)


@pytest.fixture
def ready_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把运行时置为「已就绪」的替身，并锁死「lifespan 不得运行」。"""

    runtime = SimpleNamespace(name="fake-runtime")
    monkeypatch.setattr(server, "_runtime", runtime)
    monkeypatch.setattr(
        cli,
        "_build_runtime",
        lambda settings: pytest.fail("测试不应启动 lifespan（那会真的装配图 / 拉 MCP 子进程）"),
    )
    return runtime


@pytest.fixture
def invocations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """替换 ``main._invoke``，记录入口传下来的参数并返回一个确定的 TurnResult。"""

    calls: list[dict[str, Any]] = []

    def fake_invoke(
        runtime: Any,
        message: str,
        thread_id: str,
        entrypoint: str = cli.ENTRYPOINT_CLI_SINGLE,
    ) -> cli.TurnResult:
        calls.append(
            {
                "runtime": runtime,
                "message": message,
                "thread_id": thread_id,
                "entrypoint": entrypoint,
            }
        )
        return cli.TurnResult(
            reply="已查询到成绩。",
            intent="query",
            tool_calls=("get_score",),
        )

    monkeypatch.setattr(cli, "_invoke", fake_invoke)
    return calls


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


def test_health_reports_ok_when_the_runtime_is_ready(ready_runtime: SimpleNamespace) -> None:
    response = _client().get("/api/agent/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_starting_when_the_runtime_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_runtime", None)

    response = _client().get("/api/agent/health")

    assert response.status_code == 200
    assert response.json() == {"status": "starting"}


# ---------------------------------------------------------------------------
# POST /api/agent/chat
# ---------------------------------------------------------------------------


def test_chat_returns_the_contract_the_front_end_depends_on(
    ready_runtime: SimpleNamespace, invocations: list[dict[str, Any]]
) -> None:
    """字段名与类型是前端 `@/api/agent` 的硬契约，一个字都不能改。"""

    response = _client().post("/api/agent/chat", json={"message": "帮我查一下成绩"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"reply", "intent", "toolCalls", "threadId"}
    assert body["reply"] == "已查询到成绩。"
    assert body["intent"] == "query"
    assert body["toolCalls"] == ["get_score"]
    assert isinstance(body["threadId"], str) and body["threadId"]
    # toolCalls 是 JSON 数组：tuple 必须被转成 list，否则前端 Array.isArray 会判 false
    assert isinstance(body["toolCalls"], list)


def test_chat_forwards_the_message_and_the_http_entrypoint(
    ready_runtime: SimpleNamespace, invocations: list[dict[str, Any]]
) -> None:
    """入口只做搬运：消息原样交给 ``_invoke``，并标明这次运行来自 HTTP。"""

    _client().post("/api/agent/chat", json={"message": "帮我查一下2025-2026-1的成绩"})

    assert len(invocations) == 1
    call = invocations[0]
    assert call["message"] == "帮我查一下2025-2026-1的成绩"
    assert call["entrypoint"] == ENTRYPOINT_HTTP
    assert call["runtime"] is ready_runtime


def test_chat_generates_a_thread_id_when_missing(
    ready_runtime: SimpleNamespace, invocations: list[dict[str, Any]]
) -> None:
    """不带 threadId 时自动生成：单轮调用方无须自己造会话号。"""

    first = _client().post("/api/agent/chat", json={"message": "你好"}).json()
    second = _client().post("/api/agent/chat", json={"message": "你好"}).json()

    assert first["threadId"] != second["threadId"]
    assert all(call["thread_id"] for call in invocations)


def test_chat_reuses_the_thread_id_supplied_by_the_caller(
    ready_runtime: SimpleNamespace, invocations: list[dict[str, Any]]
) -> None:
    """多轮的关键：前端带来的 threadId 必须原样传给图（不能让服务端另起一个）。"""

    response = _client().post(
        "/api/agent/chat",
        json={"message": "2025-2026第一学期", "threadId": "thread-abc"},
    )

    assert response.status_code == 200
    assert response.json()["threadId"] == "thread-abc"
    assert invocations[0]["thread_id"] == "thread-abc"


def test_chat_rejects_an_empty_message(ready_runtime: SimpleNamespace) -> None:
    """空 message 由 Pydantic 挡在入口，不进图（省掉一次模型调用）。"""

    response = _client().post("/api/agent/chat", json={"message": ""})

    assert response.status_code == 422


def test_chat_reports_503_when_the_runtime_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_runtime", None)

    response = _client().post("/api/agent/chat", json={"message": "你好"})

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"
    assert body["detail"]


def test_chat_maps_an_internal_failure_to_500_without_leaking_details(
    ready_runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行时异常 → 一句可展示的中文 + 500，内部细节只进日志。"""

    def exploding_invoke(*args: Any, **kwargs: Any) -> cli.TurnResult:
        raise RuntimeError(LEAKY_DETAIL)

    monkeypatch.setattr(cli, "_invoke", exploding_invoke)

    response = _client().post("/api/agent/chat", json={"message": "你好"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_error"
    assert LEAKY_DETAIL not in response.text
    assert "checkpoints.sqlite" not in response.text
