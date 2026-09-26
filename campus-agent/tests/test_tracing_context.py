"""Trace 项目级可观测性的测试（完全离线）。

本阶段只做一件事：让一次用户请求在追踪平台里「一眼可读」——属于哪个项目、
哪个会话、哪个入口、哪类意图。实现手段是 **LangGraph / LangChain 原生 config 的
``run_name`` / ``tags`` / ``metadata``**，没有第二套追踪机制。

因此这里验证三组事：

================================  ==================================================
要验证的事                         对应用例
================================  ==================================================
上下文怎么生成                      ``build_run_context`` 的字段集合、空值省略、
                                 入口标签、``test_invoke_wires_...`` 的入口接线
意图标签怎么进 tracing             ``annotate_intent`` 的幂等 / 规整 / 补列表 /
                                 忽略非法 config
真的进了 run（本机回环 stub）      根 run 的 name/tags/metadata、子 run 继承、
                                 intent 覆盖到 router 之后的每个 run、
                                 不同意图互不串、工具 run 以工具名命名
行为不变                           与不带上下文时逐项相同；工具 / MCP / RAG /
                                 checkpointer / 无 session 场景全部照常
================================  ==================================================

**全程不碰追踪平台在线服务**：本文件自带一个连**请求体**一起记录的回环 stub，
开启上报时 SDK 真实地把 run 发到它，于是可以逐字段断言 run 上到底写了什么。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langsmith import run_trees, utils as ls_utils

import main as cli
from app.agent.graph import INTENT_ROUTER_NODE, build_graph, create_checkpointer
from app.agent.intent import INTENTS
from app.agent.run_context import (
    ENTRYPOINT_CLI_REPL,
    ENTRYPOINT_CLI_SINGLE,
    ENTRYPOINT_TAG_PREFIX,
    INTENT_TAGS,
    INTENT_TAG_PREFIX,
    PROJECT_NAME,
    RUN_NAME,
    annotate_intent,
    build_run_context,
    intent_tag,
)
from app.config import settings as settings_module
from app.config.settings import (
    ConfigurationError,
    apply_langsmith_tracing,
    get_langsmith_settings,
)
from app.mcp.tools import make_mcp_tool
from app.tools import get_score
from tests._fakes import (
    FakeMCPClient,
    FakeRAGService,
    ScriptedChatModel,
    StaticIntentClassifier,
    make_mcp_score_tool_definition,
    score_payload,
)

#: 上报地址靠夹具钉在本机 stub，因此这里可以随便写一个形状正确的假密钥。
TEST_API_KEY = "lsv2_pt_offline-only-not-a-real-key"

#: 与追踪有关的环境变量（规范名 + 兼容旧名），夹具会整体清空与还原。
_ENV_KEYS: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_PROJECT",
    "LANGCHAIN_ENDPOINT",
    "LANGCHAIN_SESSION",
)

#: 根 run 与 intent_router 之外，所有 run 都应当带意图标签（见 run_context 模块说明）。
_RUNS_ALLOWED_WITHOUT_INTENT: frozenset[str] = frozenset({RUN_NAME, INTENT_ROUTER_NODE})


# ------------------------------------------------------------------ 本机回环 stub


class _TracingStub:
    """替代追踪平台的本机 stub：记录上报**请求体**并提供查询门面。"""

    def __init__(self, endpoint: str, bodies: list[dict[str, Any]]) -> None:
        self.endpoint = endpoint
        self._bodies = bodies

    def reset(self) -> None:
        """丢弃已记录的请求体（用于「只看这一轮」的断言）。"""

        self._bodies.clear()

    def wait_for_upload(self, timeout: float = 10.0) -> bool:
        """等后台上报线程把 run 发出来（上报是异步的，不能 invoke 完就断言）。"""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._bodies:
                return True
            time.sleep(0.05)
        return False

    def runs(self) -> dict[str, dict[str, Any]]:
        """把上报体里的 run 合并成 ``{run_id: run}``（post 与 patch 叠加）。"""

        merged: dict[str, dict[str, Any]] = {}
        for body in list(self._bodies):
            for key in ("post", "patch"):
                for item in body.get(key) or []:
                    run_id = item.get("id")
                    if run_id is not None:
                        merged.setdefault(run_id, {}).update(item)
        return merged


@pytest.fixture
def tracing_stub() -> Iterator[_TracingStub]:
    """启动本机回环 stub（随机端口）替代追踪平台在线服务。"""

    bodies: list[dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        #: 声明「不使用 multipart 端点」，让 SDK 走最简单的 ``POST /runs/batch``。
        _INFO: dict[str, Any] = {
            "version": "0.1.0-offline-stub",
            "batch_ingest_config": {
                "use_multipart_endpoint": False,
                "scale_up_qsize_trigger": 1000,
                "scale_up_nthreads_limit": 4,
                "scale_down_nempty_trigger": 4,
                "size_limit": 100,
                "size_limit_bytes": 20_971_520,
            },
            "instance_flags": {"zstd_compression_enabled": False},
        }

        def _send(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - 基类的命名约定
            self._send(self._INFO)

        def do_POST(self) -> None:  # noqa: N802 - 基类的命名约定
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                bodies.append(json.loads(raw.decode("utf-8")))
            except Exception:  # noqa: BLE001 - 非 JSON 上报体不参与断言
                bodies.append({})
            self._send({})

        def log_message(self, *args: object) -> None:
            """静默：stub 的访问日志不必混进测试输出。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield _TracingStub(f"http://127.0.0.1:{server.server_address[1]}", bodies)
    finally:
        server.shutdown()
        server.server_close()


def _reset_caches() -> None:
    """清掉「环境变量 → 判定结果」一路上的缓存，否则改了环境也不生效。"""

    get_langsmith_settings.cache_clear()
    ls_utils.get_env_var.cache_clear()
    ls_utils.get_tracer_project.cache_clear()
    run_trees._CLIENT = None  # noqa: SLF001 - 仅重置缓存，不读取其内部状态


@pytest.fixture(autouse=True)
def offline_tracing_env(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> Iterator[None]:
    """隔离环境：不读本机 ``.env``、清空追踪变量、把上报地址钉在本机 stub。

    **预置 ENDPOINT 是刻意的**：任何用例都不可能把 run 发到线上。
    """

    snapshot = {key: os.environ.get(key) for key in _ENV_KEYS}
    monkeypatch.setattr(settings_module, "load_environment", lambda: None)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", tracing_stub.endpoint)
    _reset_caches()
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _reset_caches()


def _enable_tracing(monkeypatch: pytest.MonkeyPatch, stub: _TracingStub) -> None:
    """打开上报并把地址指向本机 stub。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("LANGSMITH_PROJECT", "campus-flow-tests")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", stub.endpoint)
    assert apply_langsmith_tracing().enabled is True


# ------------------------------------------------------------------ 读 run 的小工具


def _tags(run: dict[str, Any]) -> list[str]:
    return list(run.get("tags") or [])


def _metadata(run: dict[str, Any]) -> dict[str, Any]:
    return dict((run.get("extra") or {}).get("metadata") or {})


def _by_name(runs: dict[str, dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [run for run in runs.values() if run.get("name") == name]


def _the_root(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """返回根 run：本次 invoke 的那个（没有父 run）。"""

    roots = [run for run in runs.values() if not run.get("parent_run_id")]
    assert len(roots) == 1, f"应当恰好有一个根 run，实际 {len(roots)} 个"
    return roots[0]


def _intent_tags(runs: dict[str, dict[str, Any]]) -> set[str]:
    return {
        tag
        for run in runs.values()
        for tag in _tags(run)
        if tag.startswith(INTENT_TAG_PREFIX)
    }


# ------------------------------------------------------------------ 图与场景


def _build_graph(
    *responses: AIMessage,
    tools: tuple[Any, ...] = (),
    intent: str = "chat",
    rag_service: Any = None,
    checkpointer: Any = None,
):
    """用脚本化模型构造真实图（离线、确定性）。"""

    return build_graph(
        llm=ScriptedChatModel.scripted(*responses),
        intent_classifier=StaticIntentClassifier(intent),
        rag_service=FakeRAGService() if rag_service is None else rag_service,
        tools=tools,
        checkpointer=checkpointer,
    )


def _run(
    graph: Any,
    text: str,
    *,
    session_id: str | None,
    user_id: str | None = "student-001",
    entrypoint: str | None = ENTRYPOINT_CLI_SINGLE,
) -> dict[str, Any]:
    """按生产路径调用图：config 由 ``build_run_context`` 装配。"""

    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "session_id": session_id or ""},
        config=build_run_context(
            session_id=session_id, user_id=user_id, entrypoint=entrypoint
        ),
    )


def _score_tool_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_score",
                "args": {"course": "数据结构"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )


def _summary(state: dict[str, Any]) -> list[tuple[str, Any]]:
    """把一轮结果压成可比对的形状（不含 run id 等运行期噪声）。"""

    return [
        (type(message).__name__, getattr(message, "content", None))
        for message in state["messages"]
    ]


# ================================================== 上下文怎么生成（纯函数）


def test_run_context_uses_only_native_config_fields() -> None:
    """只允许出现 LangGraph 原生字段，别的一律不加。"""

    config = build_run_context(session_id="s1", user_id="u1", entrypoint=ENTRYPOINT_CLI_SINGLE)

    assert set(config) == {"run_name", "tags", "metadata", "configurable"}
    assert config["run_name"] == RUN_NAME
    assert config["configurable"] == {"thread_id": "s1"}


def test_run_context_carries_project_session_and_entrypoint() -> None:
    """项目 / 会话 / 用户 / 入口四个维度都能被筛选到。"""

    config = build_run_context(
        session_id="sess-42", user_id="student-001", entrypoint=ENTRYPOINT_CLI_REPL
    )

    assert config["tags"] == [PROJECT_NAME, f"{ENTRYPOINT_TAG_PREFIX}{ENTRYPOINT_CLI_REPL}"]
    assert config["metadata"] == {
        "project": PROJECT_NAME,
        "session_id": "sess-42",
        "user_id": "student-001",
        "entrypoint": ENTRYPOINT_CLI_REPL,
    }


def test_run_context_omits_blank_values() -> None:
    """空值不写入：追踪里不留一堆空字符串噪声。"""

    config = build_run_context(session_id="", user_id="", entrypoint=None)

    assert config["tags"] == [PROJECT_NAME]
    assert config["metadata"] == {"project": PROJECT_NAME}
    # 没有会话就不写 thread_id：checkpointer 的会话语义不该由本模块编造
    assert "configurable" not in config


def test_run_context_without_anything_does_not_raise() -> None:
    """什么参数都不给也必须可用（直接调用图 / 尚未接入登录态的部署）。"""

    config = build_run_context()

    assert config["run_name"] == RUN_NAME
    assert config["tags"] == [PROJECT_NAME]
    assert "session_id" not in config["metadata"]


@pytest.mark.parametrize("entrypoint", [ENTRYPOINT_CLI_SINGLE, ENTRYPOINT_CLI_REPL])
def test_entrypoint_becomes_its_own_tag(entrypoint: str) -> None:
    """两种运行入口给出不同的标签，便于区分「单次提问」与「交互模式」。"""

    config = build_run_context(entrypoint=entrypoint)

    assert f"{ENTRYPOINT_TAG_PREFIX}{entrypoint}" in config["tags"]
    assert config["metadata"]["entrypoint"] == entrypoint


# ================================================== 意图标签（纯函数）


def test_intent_tags_cover_every_known_intent() -> None:
    """五种意图各有一个标签，且取值直接来自意图契约（不是第二份清单）。"""

    assert INTENT_TAGS == tuple(f"{INTENT_TAG_PREFIX}{intent}" for intent in INTENTS)
    assert INTENT_TAGS == (
        "intent:chat",
        "intent:knowledge",
        "intent:query",
        "intent:action",
        "intent:unknown",
    )


def test_intent_tag_normalises_unknown_values() -> None:
    """未知取值一律规整为 unknown，不把任意字符串灌进标签。"""

    assert intent_tag("query") == "intent:query"
    assert intent_tag("  QUERY ") == "intent:query"
    assert intent_tag("跑个流程") == "intent:unknown"
    assert intent_tag(None) == "intent:unknown"


def test_annotate_intent_appends_the_tag_once() -> None:
    """同一轮里重复标注不会把标签堆起来（节点被重放也安全）。"""

    config: RunnableConfig = {"tags": [PROJECT_NAME]}

    annotate_intent(config, "query")
    annotate_intent(config, "query")

    assert config["tags"] == [PROJECT_NAME, "intent:query"]


def test_annotate_intent_appends_to_the_caller_list() -> None:
    """追加的是**调用方传进来那个列表**：下游 run 才看得到（这是设计前提）。"""

    tags = [PROJECT_NAME]
    config: RunnableConfig = {"tags": tags}

    annotate_intent(config, "knowledge")

    assert tags == [PROJECT_NAME, "intent:knowledge"]
    assert config["tags"] is tags


def test_annotate_intent_creates_a_tag_list_when_missing() -> None:
    """没有 tags 时补一个列表，而不是静默什么都不做。"""

    config: RunnableConfig = {}

    annotate_intent(config, "chat")

    assert config["tags"] == ["intent:chat"]


def test_annotate_intent_tolerates_a_missing_config() -> None:
    """拿不到 config（例如直接调用节点函数）时不得报错。"""

    assert annotate_intent(None, "action") == "intent:action"


# ================================================== 真的进了 run（本机 stub）


def test_root_run_is_named_and_carries_the_run_context(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """根 run = 一次完整 Agent 调用：有可读的名字与项目/会话/用户/入口。"""

    _enable_tracing(monkeypatch, tracing_stub)
    graph = _build_graph(AIMessage(content="你好，我是 CampusFlow。"))

    _run(graph, "你好", session_id="sess-root")
    assert tracing_stub.wait_for_upload(), "开启上报后应当收到 run"

    root = _the_root(tracing_stub.runs())
    metadata = _metadata(root)
    assert root["name"] == RUN_NAME
    assert root["run_type"] == "chain"
    assert PROJECT_NAME in _tags(root)
    assert metadata["project"] == PROJECT_NAME
    assert metadata["session_id"] == "sess-root"
    assert metadata["user_id"] == "student-001"
    assert metadata["entrypoint"] == ENTRYPOINT_CLI_SINGLE
    # thread_id 由 LangGraph 从 configurable 自动补进 metadata
    assert metadata["thread_id"] == "sess-root"


def test_child_runs_inherit_the_run_context(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """项目标签与会话信息会传播到该次调用的全部子 run（节点 / 模型 / 工具）。"""

    _enable_tracing(monkeypatch, tracing_stub)
    graph = _build_graph(_score_tool_call(), AIMessage(content="92 分。"), tools=(get_score,), intent="query")

    _run(graph, "我数据结构考了多少分？", session_id="sess-child")
    assert tracing_stub.wait_for_upload()

    runs = tracing_stub.runs()
    assert len(runs) > 1, "除了根 run 之外还应当有子 run"
    for run in runs.values():
        assert PROJECT_NAME in _tags(run), f"{run.get('name')} 丢了项目标签"
        assert _metadata(run)["session_id"] == "sess-child"


def test_intent_tag_reaches_every_run_after_the_router(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """意图标签覆盖 router 之后的每一个 run，只有两个 run 例外。

    例外是设计的一部分（见 ``app.agent.run_context`` 的模块说明）：
    根 run 在意图算出之前就已开始，``intent_router`` 自己带不上自己的结论。
    """

    _enable_tracing(monkeypatch, tracing_stub)
    graph = _build_graph(
        _score_tool_call(), AIMessage(content="你的数据结构成绩是 92 分。"),
        tools=(get_score,), intent="query",
    )

    _run(graph, "我数据结构考了多少分？", session_id="sess-intent")
    assert tracing_stub.wait_for_upload()

    runs = tracing_stub.runs()
    missing = {
        str(run.get("name"))
        for run in runs.values()
        if "intent:query" not in _tags(run)
    }
    assert missing == {RUN_NAME, INTENT_ROUTER_NODE}
    # 决策链路上的关键 run 都带上了意图
    assert _intent_tags(runs) == {"intent:query"}


@pytest.mark.parametrize(
    ("intent", "responses", "tools"),
    [
        ("chat", (AIMessage(content="你好呀。"),), ()),
        ("knowledge", (), ()),
        ("query", (_score_tool_call(), AIMessage(content="92 分。")), (get_score,)),
        ("action", (_score_tool_call(), AIMessage(content="92 分。")), (get_score,)),
        ("unknown", (), ()),
    ],
)
def test_each_intent_gets_its_own_tag(
    monkeypatch: pytest.MonkeyPatch,
    tracing_stub: _TracingStub,
    intent: str,
    responses: tuple[AIMessage, ...],
    tools: tuple[Any, ...],
) -> None:
    """chat / knowledge / query / action / unknown 五种意图都能被区分。"""

    _enable_tracing(monkeypatch, tracing_stub)
    graph = _build_graph(*responses, tools=tools, intent=intent)

    state = _run(graph, "测试输入", session_id=f"sess-{intent}")
    assert tracing_stub.wait_for_upload()

    assert state["intent"] == intent
    assert _intent_tags(tracing_stub.runs()) == {f"{INTENT_TAG_PREFIX}{intent}"}


def test_query_and_action_stay_distinguishable(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """query 与 action 落在同一个 agent 节点上，靠标签才分得开。"""

    _enable_tracing(monkeypatch, tracing_stub)
    seen: dict[str, set[str]] = {}

    for intent in ("query", "action"):
        tracing_stub.reset()
        graph = _build_graph(
            _score_tool_call(), AIMessage(content="92 分。"),
            tools=(get_score,), intent=intent,
        )
        _run(graph, "测试输入", session_id=f"sess-{intent}")
        assert tracing_stub.wait_for_upload()
        # 两者都进了 agent 节点，光看节点名分不出意图
        assert _by_name(tracing_stub.runs(), "agent")
        seen[intent] = _intent_tags(tracing_stub.runs())

    assert seen["query"] == {"intent:query"}
    assert seen["action"] == {"intent:action"}


def test_different_intents_do_not_leak_into_each_other(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """不同意图的标签不会互相串（每轮 config 独立）。"""

    _enable_tracing(monkeypatch, tracing_stub)

    tracing_stub.reset()
    _run(_build_graph(AIMessage(content="你好呀。")), "你好", session_id="sess-chat")
    assert tracing_stub.wait_for_upload()
    chat_runs = tracing_stub.runs()

    tracing_stub.reset()
    _run(
        _build_graph(intent="knowledge"),
        "图书馆几点开门？",
        session_id="sess-knowledge",
    )
    assert tracing_stub.wait_for_upload()
    knowledge_runs = tracing_stub.runs()

    assert _intent_tags(chat_runs) == {"intent:chat"}
    assert _intent_tags(knowledge_runs) == {"intent:knowledge"}
    assert "intent:knowledge" not in _intent_tags(chat_runs)
    assert "intent:chat" not in _intent_tags(knowledge_runs)


def test_repl_turns_keep_their_own_intent_tags(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """同一会话（thread_id 不变）的多轮：上一轮的意图标签不会带到下一轮。"""

    _enable_tracing(monkeypatch, tracing_stub)
    # 两个图共用同一个 checkpointer，模拟「同一会话里换了识别结果」的场景
    saver = create_checkpointer("memory")
    graph = build_graph(
        llm=ScriptedChatModel.scripted(AIMessage(content="你好呀。")),
        intent_classifier=StaticIntentClassifier("chat"),
        rag_service=FakeRAGService(),
        checkpointer=saver,
    )

    tracing_stub.reset()
    _run(graph, "你好", session_id="sess-repl", entrypoint=ENTRYPOINT_CLI_REPL)
    assert tracing_stub.wait_for_upload()
    first = tracing_stub.runs()

    # 第二轮换成 knowledge：同一个 thread_id，但 config 是新的
    tracing_stub.reset()
    knowledge_graph = build_graph(
        llm=ScriptedChatModel.scripted(),
        intent_classifier=StaticIntentClassifier("knowledge"),
        rag_service=FakeRAGService(),
        checkpointer=saver,
    )
    _run(knowledge_graph, "图书馆几点开门？", session_id="sess-repl", entrypoint=ENTRYPOINT_CLI_REPL)
    assert tracing_stub.wait_for_upload()
    second = tracing_stub.runs()

    assert _intent_tags(first) == {"intent:chat"}
    assert _intent_tags(second) == {"intent:knowledge"}
    # 入口标签在两轮里都稳定存在
    for runs in (first, second):
        assert f"{ENTRYPOINT_TAG_PREFIX}{ENTRYPOINT_CLI_REPL}" in _tags(_the_root(runs))


def test_tool_run_is_named_after_the_tool(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """工具执行的 run 直接以工具名命名（get_exam_schedule / get_course_schedule / get_score）。"""

    _enable_tracing(monkeypatch, tracing_stub)
    graph = _build_graph(
        _score_tool_call(), AIMessage(content="92 分。"), tools=(get_score,), intent="query"
    )

    _run(graph, "我数据结构考了多少分？", session_id="sess-tool")
    assert tracing_stub.wait_for_upload()

    runs = tracing_stub.runs()
    tool_runs = [run for run in runs.values() if run.get("run_type") == "tool"]
    assert [run["name"] for run in tool_runs] == ["get_score"]
    assert "intent:query" in _tags(tool_runs[0])
    # 工具执行节点本身也在 run 树上
    assert _by_name(runs, "tools")


def test_mcp_tool_keeps_its_tool_name_in_the_trace(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """经 MCP 提供的工具同样以工具名命名（适配器不改名，trace 里能直接看到）。"""

    _enable_tracing(monkeypatch, tracing_stub)
    client = FakeMCPClient(
        tools=(make_mcp_score_tool_definition(),), payload=score_payload()
    )
    mcp_tool = make_mcp_tool(
        client, client.find_tool("get_score"), bound_arguments={"student_id": "student-001"}
    )
    graph = _build_graph(
        _score_tool_call(), AIMessage(content="92 分。"), tools=(mcp_tool,), intent="query"
    )

    state = _run(graph, "我数据结构考了多少分？", session_id="sess-mcp")
    assert tracing_stub.wait_for_upload()

    runs = tracing_stub.runs()
    tool_runs = [run for run in runs.values() if run.get("run_type") == "tool"]
    assert [run["name"] for run in tool_runs] == ["get_score"]
    assert "intent:query" in _tags(tool_runs[0])
    # MCP 链路本身没被影响：工具确实被调用，结果回到消息历史
    assert client.calls and client.calls[0][0] == "get_score"
    assert any(isinstance(message, ToolMessage) for message in state["messages"])


# ================================================== 行为不变


def test_answers_are_identical_with_and_without_the_run_context() -> None:
    """带上下文与不带上下文两次跑，消息序列逐项相同。"""

    def responses() -> tuple[AIMessage, AIMessage]:
        return (_score_tool_call(), AIMessage(content="你的数据结构成绩是 92 分。"))

    plain = _build_graph(*responses(), tools=(get_score,), intent="query")
    state_plain = plain.invoke(
        {"messages": [HumanMessage(content="我数据结构考了多少分？")]},
        config={"configurable": {"thread_id": "no-context"}},
    )

    contextualised = _build_graph(*responses(), tools=(get_score,), intent="query")
    state_contextualised = _run(
        contextualised, "我数据结构考了多少分？", session_id="with-context"
    )

    assert _summary(state_contextualised) == _summary(state_plain)
    assert state_contextualised["intent"] == state_plain["intent"] == "query"
    assert state_contextualised["messages"][-1].content == "你的数据结构成绩是 92 分。"


def test_knowledge_run_keeps_sources_and_carries_the_intent_tag(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """RAG 链路不受影响：引用来源照常写回，只是多了一个意图标签。"""

    _enable_tracing(monkeypatch, tracing_stub)
    rag_service = FakeRAGService(
        sources=({"source": "campus_rules.md", "title": "校园规则", "score": 0.72},)
    )
    graph = _build_graph(rag_service=rag_service, intent="knowledge")

    state = _run(graph, "图书馆几点开门？", session_id="sess-rag")
    assert tracing_stub.wait_for_upload()

    assert state["messages"][-1].content == rag_service.answer_text
    assert [source["source"] for source in state["sources"]] == ["campus_rules.md"]
    knowledge_runs = _by_name(tracing_stub.runs(), "knowledge")
    assert knowledge_runs and "intent:knowledge" in _tags(knowledge_runs[0])


def test_checkpointer_still_isolates_sessions(
    monkeypatch: pytest.MonkeyPatch, tracing_stub: _TracingStub
) -> None:
    """会话隔离不受影响：同 thread_id 累积历史，不同 thread_id 互不可见。"""

    _enable_tracing(monkeypatch, tracing_stub)
    graph = _build_graph(
        AIMessage(content="第一轮回答。"),
        AIMessage(content="第二轮回答。"),
        AIMessage(content="另一个会话的回答。"),
        checkpointer=create_checkpointer("memory"),
    )

    first = _run(graph, "第一轮", session_id="thread-a")
    second = _run(graph, "第二轮", session_id="thread-a")
    other = _run(graph, "别的会话", session_id="thread-b")

    # thread-a 第二轮带着第一轮的历史（1 用户 + 1 助手 + 1 用户 + 1 助手）
    assert len(second["messages"]) == 4
    # thread-b 只有自己那一轮
    assert len(other["messages"]) == 2
    assert len(first["messages"]) == 2
    assert tracing_stub.wait_for_upload()
    # 会话信息确实进了 tracing
    sessions = {
        _metadata(run).get("session_id")
        for run in tracing_stub.runs().values()
    }
    assert {"thread-a", "thread-b"} <= sessions


def test_graph_runs_without_session_or_user() -> None:
    """没有 session_id / user_id 时照常跑完，不因缺字段报错。"""

    graph = _build_graph(AIMessage(content="你好，我是 CampusFlow。"))

    state = _run(graph, "你好", session_id=None, user_id=None, entrypoint=None)

    assert state["messages"][-1].content == "你好，我是 CampusFlow。"
    assert state["intent"] == "chat"


def test_no_second_tracing_mechanism_in_the_application_layer() -> None:
    """只允许用 config 的原生字段：不得注册回调、不得自己取/建 run。"""

    app_root = Path(settings_module.app_root()) / "app"
    forbidden = (
        "BaseCallbackHandler",
        "CallbackManager",
        "get_current_run_tree",
        "add_metadata",
    )
    offenders: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(token in source for token in forbidden):
            offenders.append(str(path.relative_to(app_root.parent)))

    assert offenders == []


# ================================================== 入口接线


class _RecordingRuntime:
    """记录 ``invoke`` 收到的 (input, config)，返回最小可用状态。"""

    def __init__(self, *, intent: str = "chat", reply: str = "好的。") -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self._intent = intent
        self._reply = reply

    def invoke(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((payload, config or {}))
        return {
            "messages": [AIMessage(content=self._reply)],
            "intent": self._intent,
            "sources": [],
        }


def test_invoke_wires_the_run_context_into_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动入口真的把它装配进 config（而不是只有构建函数存在）。"""

    monkeypatch.setattr(
        cli, "get_campus_api_settings", lambda: SimpleNamespace(default_student_id="student-007")
    )
    runtime = _RecordingRuntime()

    result = cli._invoke(runtime, "你好", "sess-wire", ENTRYPOINT_CLI_REPL)

    payload, config = runtime.calls[0]
    assert payload["session_id"] == "sess-wire"
    assert config["run_name"] == RUN_NAME
    assert config["configurable"] == {"thread_id": "sess-wire"}
    assert config["tags"] == [PROJECT_NAME, f"{ENTRYPOINT_TAG_PREFIX}{ENTRYPOINT_CLI_REPL}"]
    assert config["metadata"]["session_id"] == "sess-wire"
    assert config["metadata"]["user_id"] == "student-007"
    assert config["metadata"]["entrypoint"] == ENTRYPOINT_CLI_REPL
    assert result.reply == "好的。"


def test_invoke_omits_user_id_when_campus_settings_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拿不到用户身份时省略该字段，而不是让整轮请求失败。"""

    def _unavailable() -> Any:
        raise ConfigurationError("校园业务服务未配置")

    monkeypatch.setattr(cli, "get_campus_api_settings", _unavailable)
    runtime = _RecordingRuntime()

    cli._invoke(runtime, "你好", "sess-no-user")

    _, config = runtime.calls[0]
    assert "user_id" not in config["metadata"]
    assert cli._tracing_user_id() == ""


def test_both_entrypoints_are_wired_to_their_own_tag() -> None:
    """单次提问与交互模式各自传入自己的入口标识。"""

    source = (Path(settings_module.app_root()) / "main.py").read_text(encoding="utf-8")
    calls = [line.strip() for line in source.splitlines() if "_invoke(runtime," in line]

    assert len(calls) == 2, f"应当只有两处调用点，实际 {calls}"
    assert any("ENTRYPOINT_CLI_SINGLE" in call for call in calls)
    assert any("ENTRYPOINT_CLI_REPL" in call for call in calls)
