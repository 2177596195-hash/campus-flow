"""LangSmith 追踪接入的测试（完全离线）。

本阶段的接入方式是官方推荐的那一种：**只配置环境变量**，追踪由
LangChain / LangGraph 自动完成（``langsmith.utils.tracing_is_enabled()``
在每次运行之前读环境变量）。因此这里要验证的不是「我们写了多少 tracing 代码」，
而是四件事：

=========================  =========================================================
要验证的事                  对应用例
=========================  =========================================================
未配置也能跑                ``test_defaults_are_disabled`` /
                           ``test_apply_disables_tracing_and_sdk_agrees`` /
                           ``test_graph_runs_normally_with_tracing_off``
开关读得对                  ``test_switch_values_are_parsed`` /
                           ``test_enabled_requires_an_api_key`` /
                           ``test_legacy_sdk_names_are_honoured``
项目名与地址                ``test_project_defaults_to_the_sdk_default`` /
                           ``test_endpoint_is_normalised``
配置错了也不阻断启动        ``test_invalid_switch_degrades_instead_of_raising`` /
                           ``test_invalid_endpoint_degrades_instead_of_raising``
Key 不外泄                  ``test_api_key_never_appears_in_repr`` /
                           ``test_api_key_never_appears_in_logs``
开启后行为不变              ``test_tracing_on_yields_a_trace_and_the_same_answer`` /
                           ``test_tracing_on_does_not_change_tool_calling`` /
                           ``test_stale_legacy_switch_cannot_re_enable`` /
                           ``test_apply_clears_sdk_cached_env_reads``
接入点只有一个              ``test_application_code_has_no_langsmith_usage`` /
                           ``test_startup_applies_tracing_exactly_once``
=========================  =========================================================

**全程不碰 LangSmith 在线服务**：所有「开启追踪」的用例都把
``LANGSMITH_ENDPOINT`` 指向本机回环 stub（``ThreadingHTTPServer``），
SDK 真实地把 run 上报到这个 stub——既证明了「确实产生 trace」，
又不产生任何外网流量。autouse 夹具还会**预置** stub 地址并清空其余变量，
因此本文件里不存在「误连线上」的路径。

被替换的只有「环境变量怎么来」这一处：Agent 图、工具、模型替身跑的都是真实代码。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langsmith import run_trees
from langsmith import utils as langsmith_utils

from app.agent.graph import build_graph
from app.config import settings as settings_module
from app.config.settings import (
    ConfigurationError,
    LangSmithSettings,
    apply_langsmith_tracing,
    get_langsmith_settings,
)
from app.tools import get_score
from tests._fakes import (
    FakeRAGService,
    ScriptedChatModel,
    StaticIntentClassifier,
)

#: 假的 API Key：形状与真实密钥相似，但只用于「它绝不能出现在日志里」这类断言。
TEST_API_KEY = "lsv2_pt_offline-only-not-a-real-key"

#: 断言「密钥不外泄」时使用的片段：连前缀也不允许出现。
TEST_API_KEY_PREFIX = TEST_API_KEY[:8]

TEST_PROJECT = "campus-flow-tests"

#: 与追踪有关的全部环境变量：规范名 + 官方 SDK 仍兼容的旧名
_LANGSMITH_KEYS: tuple[str, ...] = (
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

#: 「本项目的日志」前缀。断言密钥不外泄时用它把 SDK 自身产生的日志摘出去——
#: 那些日志由 langsmith 包控制，不是本阶段能负责的代码。
_OUR_LOGGER_PREFIXES: tuple[str, ...] = ("app", "campus_flow", "main", "__main__")

#: 允许提到 LangSmith 的**单文件豁免**清单（相对 ``app/`` 的路径分段）。
#: 这两个文件分别用的是 LangSmith 的 Datasets API（同步评估数据集）与
#: Evaluation API（把本地指标接进实验）——都是由 CLI 显式触发的运维 / 评估动作，
#: 与 tracing 的接入方式（只写环境变量、业务代码零调用）是两件不同的事。
#: 豁免是单文件的，且守卫会额外断言它们不含任何 tracing 专有 API。
_LANGSMITH_EXEMPT_MODULES: tuple[tuple[str, ...], ...] = (
    ("app", "rag", "evaluation", "langsmith_dataset.py"),
    ("app", "rag", "evaluation", "langsmith_evaluators.py"),
)

#: tracing 专有 API：出现在被豁免文件里即视为「借豁免偷偷加第二套追踪机制」。
_TRACING_ONLY_TOKENS: tuple[str, ...] = (
    "@traceable",
    "traceable",
    "run_trees",
    "LangChainTracer",
    "tracing_is_enabled",
    "tracing_v2_enabled",
    "TRACING_V2",
    "add_runs",
)


# ------------------------------------------------------------------ 本机回环 stub


class _StubHandler(BaseHTTPRequestHandler):
    """替代 LangSmith 在线服务的本机 stub：只记录「收到了什么」。"""

    protocol_version = "HTTP/1.1"

    #: 由夹具注入的记录容器 ``(方法, 路径)``
    recorder: ClassVar[list[tuple[str, str]]] = []

    #: ``GET /info`` 的应答。声明「不使用 multipart 端点」，让 SDK 走最简单的
    #: ``POST /runs/batch``，也避免它因为缺少 zstd 压缩能力而打警告。
    _INFO: ClassVar[dict[str, Any]] = {
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

    def _respond(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - 基类的命名约定
        self.recorder.append(("GET", self.path))
        self._respond(self._INFO)

    def do_POST(self) -> None:  # noqa: N802 - 基类的命名约定
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.recorder.append(("POST", self.path))
        self._respond({})

    def log_message(self, *args: object) -> None:
        """静默：stub 的访问日志不必混进测试输出。"""


class _LangSmithStub:
    """stub 的门面：上报地址 + 收到的请求。"""

    def __init__(self, endpoint: str, recorder: list[tuple[str, str]]) -> None:
        self.endpoint = endpoint
        self._recorder = recorder

    @property
    def requests(self) -> list[tuple[str, str]]:
        return list(self._recorder)

    def uploads(self) -> list[str]:
        """已收到的上报请求路径（只有 ``POST`` 才算上报）。"""

        return [path for method, path in self._recorder if method == "POST"]

    def wait_for_upload(self, timeout: float = 10.0) -> bool:
        """等后台上报线程把 run 发出来。

        上报是异步的（SDK 在后台线程里批量发送），因此断言「产生了 trace」
        必须先等它，而不是 invoke 返回就立刻检查。
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.uploads():
                return True
            time.sleep(0.05)
        return False


@pytest.fixture
def langsmith_stub() -> Iterator[_LangSmithStub]:
    """启动本机回环 stub（随机端口）替代 LangSmith 在线服务。"""

    _StubHandler.recorder = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield _LangSmithStub(endpoint, _StubHandler.recorder)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def isolated_langsmith_env(
    monkeypatch: pytest.MonkeyPatch, langsmith_stub: _LangSmithStub
) -> Iterator[None]:
    """隔离环境：不读本机 ``.env``、清空 LangSmith 变量、把上报地址钉在本机 stub。

    **预置 ENDPOINT 是刻意的**：这样即使某个用例只开了开关、忘了给地址，
    也不可能打到线上。
    """

    snapshot = {key: os.environ.get(key) for key in _LANGSMITH_KEYS}
    monkeypatch.setattr(settings_module, "load_environment", lambda: None)
    for key in _LANGSMITH_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", langsmith_stub.endpoint)
    _reset_caches()
    try:
        yield
    finally:
        # apply_langsmith_tracing 是直接写 os.environ 的，monkeypatch 不会回滚这些写入：
        # 不手动还原的话，本文件「开了追踪」的用例会把状态漏给后面所有用例。
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _reset_caches()


def _reset_caches() -> None:
    """清掉「环境变量 → 判定结果」一路上的所有缓存。

    - ``get_langsmith_settings``：本项目配置入口（``lru_cache``）；
    - ``langsmith.utils`` 的两个读取函数：SDK 侧缓存；
    - ``run_trees._CLIENT``：SDK 的全局客户端单例，缓存了 API 地址与后台上报
      线程——不重置的话，本文件第二个「开启追踪」的用例会继续打到上一个用例
      已经关闭的端口。

    这些都是 SDK 的既有缓存，测试里必须显式清掉，「环境变量变了」才可见。
    """

    get_langsmith_settings.cache_clear()
    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()
    run_trees._CLIENT = None  # noqa: SLF001 - 仅重置缓存，不读取其内部状态


def _enable_tracing(
    monkeypatch: pytest.MonkeyPatch,
    stub: _LangSmithStub,
    *,
    api_key: str = TEST_API_KEY,
    project: str = TEST_PROJECT,
) -> LangSmithSettings:
    """开启追踪（上报地址由夹具指向本机 stub）并返回生效配置。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", api_key)
    monkeypatch.setenv("LANGSMITH_PROJECT", project)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", stub.endpoint)
    return apply_langsmith_tracing()


# ------------------------------------------------------------------ 图与工具


def _build_graph(*responses: AIMessage, tools: tuple[Any, ...] = (), intent: str = "chat"):
    """用脚本化模型构造真实图（离线、确定性）。"""

    llm = ScriptedChatModel.scripted(*responses)
    graph = build_graph(
        llm=llm,
        intent_classifier=StaticIntentClassifier(intent),
        rag_service=FakeRAGService(),
        tools=tools,
    )
    return graph, llm


def _run(graph: Any, text: str, thread_id: str) -> dict[str, Any]:
    return graph.invoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def _summary(state: dict[str, Any]) -> list[tuple[str, Any]]:
    """把一轮结果压成可比对的形状：消息类型 + 内容（不含 run id 等运行期噪声）。"""

    return [
        (type(message).__name__, getattr(message, "content", None))
        for message in state["messages"]
    ]


# ======================================================== 未配置：照常可跑


def test_defaults_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """什么都不配时：开关为假、最终未启用、项目名取 SDK 默认值、地址为空。"""

    # 夹具默认把 ENDPOINT 预置成本机 stub（保证不会误连线上），这条用例要的
    # 正是「完全没配」的状态，所以显式去掉它。
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    get_langsmith_settings.cache_clear()

    settings = get_langsmith_settings()

    assert settings.requested is False
    assert settings.enabled is False
    assert settings.api_key == ""
    # 不自造项目名：未配置时与官方 SDK 的默认值一致（"default"）
    assert settings.project == "default"
    # 空串表示「沿用官方 SDK 的默认上报地址」，本项目不复制一份地址
    assert settings.endpoint == ""


def test_apply_disables_tracing_and_sdk_agrees() -> None:
    """未配置时 apply 不报错、把开关写成 SDK 认得的 ``false``，SDK 也判为未启用。"""

    resolved = apply_langsmith_tracing()

    assert resolved.enabled is False
    assert os.environ["LANGSMITH_TRACING"] == "false"
    # 直接问 SDK：它才是真正决定「要不要上报」的一方
    assert langsmith_utils.tracing_is_enabled() is False


def test_graph_runs_normally_with_tracing_off(
    langsmith_stub: _LangSmithStub,
) -> None:
    """未配置追踪时图照常跑完，且没有任何上报。"""

    apply_langsmith_tracing()
    graph, _ = _build_graph(AIMessage(content="你好，我是 CampusFlow。"))

    state = _run(graph, "你好", "tracing-off")

    assert state["messages"][-1].content == "你好，我是 CampusFlow。"
    assert langsmith_stub.uploads() == []


# ============================================================ 开关怎么读


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_switch_values_are_accepted(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真值写法（大小写、1/yes/on）都能开起来——配置层比 SDK 宽松，所以必须归一化。"""

    monkeypatch.setenv("LANGSMITH_TRACING", raw)
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)

    settings = get_langsmith_settings()

    assert settings.requested is True
    assert settings.enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_falsy_switch_values_are_accepted(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """假值写法一律视为关闭，且不会因为「配了密钥」而被打开。"""

    monkeypatch.setenv("LANGSMITH_TRACING", raw)
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)

    settings = get_langsmith_settings()

    assert settings.requested is False
    assert settings.enabled is False


def test_enabled_requires_an_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """只开开关不配 Key：降级为关闭并告警，而不是让每轮都在后台鉴权失败。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    caplog.set_level(logging.WARNING, logger="app.config.settings")

    settings = get_langsmith_settings()

    assert settings.requested is True
    assert settings.enabled is False
    assert "API Key" in caplog.text


def test_legacy_sdk_names_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """只用官方旧名（``LANGCHAIN_*``）配置也能开起来，并与 SDK 的判定一致。"""

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("LANGCHAIN_PROJECT", "legacy-project")

    settings = apply_langsmith_tracing()

    assert settings.enabled is True
    assert settings.project == "legacy-project"
    assert langsmith_utils.tracing_is_enabled() is True
    # 归一化：规范名被补齐，且会「盖过」取值的旧名被清掉
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == TEST_API_KEY
    assert "LANGCHAIN_TRACING_V2" not in os.environ


def test_project_defaults_to_the_sdk_default() -> None:
    """未配置项目名时用 SDK 的默认项目名，不去自造一个「更像本项目」的名字。"""

    assert get_langsmith_settings().project == "default"


def test_endpoint_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """上报地址去掉尾部斜杠；纯空白等价于未配置。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)

    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://example.invalid/api/")
    assert get_langsmith_settings().endpoint == "https://example.invalid/api"

    monkeypatch.setenv("LANGSMITH_ENDPOINT", "   ")
    get_langsmith_settings.cache_clear()  # 配置入口带 lru_cache，换环境后要清
    assert get_langsmith_settings().endpoint == ""


# ==================================================== 配置错了也不阻断启动


def test_invalid_switch_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """开关写错（如 ``maybe``）：配置层拒绝，但 apply 只降级、不抛给主流程。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "maybe")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)

    with pytest.raises(ConfigurationError) as excinfo:
        get_langsmith_settings()
    assert "LANGSMITH_TRACING" in str(excinfo.value)

    caplog.set_level(logging.WARNING, logger="app.config.settings")
    resolved = apply_langsmith_tracing()

    assert resolved.enabled is False
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert langsmith_utils.tracing_is_enabled() is False


def test_invalid_endpoint_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """地址写错（非 http(s)）同样只降级；错误信息里不得出现 API Key。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "ftp://example.invalid")

    with pytest.raises(ConfigurationError) as excinfo:
        get_langsmith_settings()
    message = str(excinfo.value)
    assert "LANGSMITH_ENDPOINT" in message
    assert TEST_API_KEY not in message

    resolved = apply_langsmith_tracing()

    assert resolved.enabled is False
    assert langsmith_utils.tracing_is_enabled() is False


# ============================================================== Key 不外泄


def test_api_key_never_appears_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置对象的 ``repr`` / ``str`` 只说明「已配置」，不带出密钥（含前缀）。"""

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)

    settings = get_langsmith_settings()
    texts = [repr(settings), str(settings), f"{settings!r}", repr(
        {"langsmith": settings}
    )]

    for text in texts:
        assert TEST_API_KEY not in text
        assert TEST_API_KEY_PREFIX not in text
    assert "已配置" in texts[0]


def test_api_key_never_appears_in_logs(
    monkeypatch: pytest.MonkeyPatch,
    langsmith_stub: _LangSmithStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """开一轮带追踪的真实运行，本项目自己打的日志里不得出现密钥。

    只断言「我们的」日志（``app.*`` / ``campus_flow``）：
    SDK 内部的日志由 langsmith 包控制，不在本阶段能负责的范围里。
    """

    caplog.set_level(logging.DEBUG)
    _enable_tracing(monkeypatch, langsmith_stub)

    graph, _ = _build_graph(AIMessage(content="你好，我是 CampusFlow。"))
    _run(graph, "你好", "tracing-logs")
    langsmith_stub.wait_for_upload()

    ours = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(_OUR_LOGGER_PREFIXES)
    ]

    assert ours, "应当至少捕获到一条本项目自己的日志"
    for message in ours:
        assert TEST_API_KEY not in message
        assert TEST_API_KEY_PREFIX not in message


# ================================================== 开启追踪：行为不变 + 真有 trace


def test_tracing_on_yields_a_trace_and_the_same_answer(
    monkeypatch: pytest.MonkeyPatch, langsmith_stub: _LangSmithStub
) -> None:
    """同一段对话，关与开两次跑：回答与消息序列逐项相同，且开启时确实上报了。"""

    graph_off, _ = _build_graph(AIMessage(content="你好，我是 CampusFlow。"))
    apply_langsmith_tracing()
    state_off = _run(graph_off, "你好", "tracing-compare-off")
    assert langsmith_stub.uploads() == []

    _enable_tracing(monkeypatch, langsmith_stub)
    graph_on, _ = _build_graph(AIMessage(content="你好，我是 CampusFlow。"))
    state_on = _run(graph_on, "你好", "tracing-compare-on")

    assert _summary(state_on) == _summary(state_off)
    assert state_on["intent"] == state_off["intent"] == "chat"
    assert langsmith_stub.wait_for_upload(), "开启追踪后应当收到上报"
    assert "/runs" in langsmith_stub.uploads()[0]


def test_tracing_on_does_not_change_tool_calling(
    monkeypatch: pytest.MonkeyPatch,
    langsmith_stub: _LangSmithStub,
) -> None:
    """工具链路（模型请求工具 → ToolNode 执行 → 模型汇总）在开启追踪后结果不变。

    校园业务服务的替身由全局 ``conftest`` 注入，这里不需要再替换——
    本文件要证明的是「追踪开关不影响业务」，其余依赖与平时完全一致。
    """

    def responses() -> tuple[AIMessage, AIMessage]:
        return (
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_score",
                        "args": {"course": "数据结构"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="你的数据结构成绩是 92 分。"),
        )

    graph_off, _ = _build_graph(*responses(), tools=(get_score,), intent="query")
    apply_langsmith_tracing()
    state_off = _run(graph_off, "我数据结构考了多少分？", "tools-off")
    assert langsmith_stub.uploads() == []

    _enable_tracing(monkeypatch, langsmith_stub)
    graph_on, _ = _build_graph(*responses(), tools=(get_score,), intent="query")
    state_on = _run(graph_on, "我数据结构考了多少分？", "tools-on")

    assert _summary(state_on) == _summary(state_off)
    assert any(isinstance(m, ToolMessage) for m in state_on["messages"])
    assert state_on["messages"][-1].content == "你的数据结构成绩是 92 分。"
    assert langsmith_stub.wait_for_upload(), "工具链路同样应当产生 trace"


def test_stale_legacy_switch_cannot_re_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置里关闭时，环境里残留的旧开关（``*_TRACING_V2``）不能把它偷偷打开。

    这是「配置关闭时保持当前行为」的直接证明：SDK 判断开关时优先读
    ``*_TRACING_V2``，所以 apply 必须把它们清掉。
    """

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TRACING_V2", "true")

    resolved = apply_langsmith_tracing()

    assert resolved.enabled is False
    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGSMITH_TRACING_V2" not in os.environ
    assert langsmith_utils.tracing_is_enabled() is False


def test_apply_clears_sdk_cached_env_reads(
    monkeypatch: pytest.MonkeyPatch, langsmith_stub: _LangSmithStub
) -> None:
    """先被 SDK 读过的开关值不能把最终判定钉死。

    ``langsmith.utils.get_env_var`` 是 ``lru_cache``。这里先把缓存喂成「开着」，
    再清空环境变量后调用 apply —— 只有 apply 主动清了 SDK 的缓存，
    SDK 才会看到「关闭」，否则它会一直以为自己开着并继续上报。
    """

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", TEST_API_KEY)
    _reset_caches()
    assert langsmith_utils.tracing_is_enabled() is True  # 把 SDK 缓存喂成「开」

    for key in _LANGSMITH_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_langsmith_settings.cache_clear()

    resolved = apply_langsmith_tracing()

    assert resolved.enabled is False
    assert langsmith_utils.tracing_is_enabled() is False


# ======================================================= 接入点只有一个


def test_application_code_has_no_langsmith_usage() -> None:
    """业务代码里不得出现 LangSmith 调用（唯一接入点是 ``app/config/``）。

    开启追踪只需环境变量，任何在节点 / 图 / 工具 / RAG / MCP 里写 tracing 代码
    的做法都是第二套机制。这条守卫把「接入点只有一个」变成可执行的约束：
    ``app/config/`` 之外的部分（对话、图、节点、工具、RAG、MCP）一个字都不许提。

    **单文件豁免**（见 ``_LANGSMITH_EXEMPT_MODULES``）：LangSmith 的 Datasets /
    Evaluation API 是另一回事——它们由 CLI 显式触发、只搬数据集与跑评估、
    不上报 trace，却必然要出现 ``langsmith`` 这个词。豁免只覆盖那两个文件，
    并且下面会额外断言它们**不含任何 tracing 专有 API**（``_TRACING_ONLY_TOKENS``），
    所以「业务代码里没有第二套 tracing 机制」这条约束并未放松。
    """

    app_root = Path(settings_module.app_root())
    # 配置包是唯一允许提到 LangSmith 的地方：settings.py 读配置并落地，
    # __init__.py 只是把它再导出一次。
    allowed_dir = app_root / "app" / "config"
    allowed_files = {app_root.joinpath(*parts) for parts in _LANGSMITH_EXEMPT_MODULES}
    offenders: list[str] = []
    for path in sorted((app_root / "app").rglob("*.py")):
        if path.parent == allowed_dir or path in allowed_files:
            continue
        source = path.read_text(encoding="utf-8")
        if any(
            token in source
            for token in ("langsmith", "LangSmith", "tracing_v2_enabled", "@traceable", "TRACING_V2")
        ):
            offenders.append(str(path.relative_to(app_root)))

    assert offenders == []

    # 豁免文件不是免检：它们只能碰 Datasets / Evaluation API，出现 tracing API 即视为越界。
    for allowed_file in sorted(allowed_files):
        exempt_source = allowed_file.read_text(encoding="utf-8")
        leaked = [token for token in _TRACING_ONLY_TOKENS if token in exempt_source]
        assert leaked == [], f"{allowed_file.name} 出现了 tracing 专有 API: {leaked}"


def test_startup_applies_tracing_exactly_once() -> None:
    """启动入口只调用一次 apply：三类运行方式（单次 / 交互 / 索引）都经由它。"""

    source = (Path(settings_module.app_root()) / "main.py").read_text(encoding="utf-8")

    assert source.count("apply_langsmith_tracing()") == 1
