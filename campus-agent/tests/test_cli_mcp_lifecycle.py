"""CLI 收尾时的 MCP 客户端生命周期（P4：生产资源收尾）。完全离线。

背景（审计发现 P4）：``main.py`` 的 ``_close_runtime()`` 此前只关闭检查点后端。
生产运行时由装配期的 ``build_agent_tools()`` 从进程内单例取用的 ``StdioMCPClient``，
首次工具发现时会拉起一个 MCP Server 子进程，并独占一条后台事件循环线程与
一对 stdio 管道——这些资源**没有明确的生产关闭路径**，只是被 daemon 线程 /
进程退出间接结束。修复即「收尾时复用 ``reset_mcp_client()``」，本文件把它钉成
可执行契约，覆盖：

======================  ==========================================================
用例编号                 被守住的结论
======================  ==========================================================
1                       正常单轮退出 → 客户端被关闭、缓存被清空
2                       正常 REPL 退出 → 客户端被关闭
3                       会话进行中客户端**保持打开**，收尾不提前（只在退出时发生）
4                       运行期异常（单轮 / REPL 某一轮）→ 客户端仍被关闭
5                       检查点后端照旧被关闭（两条收尾互不拖累）
6                       任一处收尾失败，另一处仍执行
7                       close 次数合理：一次运行恰好一次；重复收尾不再关第二次
8                       没有客户端时关闭流程仍然安全（缓存为空 / MCP 配置坏掉 / MCP 关闭）
9                       不越界：收尾只作用于进程内单例，不会去关别的实例
10                      真实子进程：收尾后 MCP Server 子进程与后台线程确实结束
======================  ==========================================================

测试手法与 ``tests/mcp/test_client_singleton.py``、``tests/test_cli_single_turn.py``
一致：MCP 配置用替身注入、``main()`` 的周边依赖全部替身化，因此**不读环境变量、
不连 Spring Boot**（``StdioMCPClient`` 构造本身零副作用，建连发生在 ``start()``）。
唯一例外是第 10 组：它按 ``tests/mcp/test_client_stdio.py`` 的配方真实拉起一次
MCP Server 子进程，用来证明「收尾不是只置了个标志位」。真实协议往返的完整覆盖
仍在 ``tests/mcp/test_client_stdio.py``，本次改动不触碰那条链路。
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import main as cli
from app.agent.graph import create_checkpointer
from app.config.settings import ConfigurationError, app_root
from app.mcp import client as client_module
from app.mcp import tools as mcp_tools_module
from app.mcp.client import StdioMCPClient, get_mcp_client, reset_mcp_client
from tests._fakes import make_mcp_settings

# =========================================================== 夹具与替身


@pytest.fixture
def mcp_singleton(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """给 MCP 单例一个受控配置来源，并保证进入 / 离开时缓存都是干净的。

    与 ``tests/mcp/test_client_singleton.py::isolated_singleton`` 同一手法：
    配置替身注入（完全不读环境变量），前后各 reset 一次（不把别的用例留下的实例
    串进来，也不把自己的残留留给后面的用例）。
    """

    monkeypatch.setattr(client_module, "get_mcp_settings", lambda: make_mcp_settings())
    reset_mcp_client()
    yield
    reset_mcp_client()


@pytest.fixture
def no_mcp_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """缓存为空，且把 MCP 配置换成「一读就炸」——用来证明收尾不会去读配置。

    若 ``reset_mcp_client()`` 在缓存为空时仍然尝试装配客户端（例如写成无条件
    ``get_mcp_client().close()``），这里会直接抛 ``ConfigurationError``，
    用例随即失败——这正是「没有客户端时收尾是否安全」要守住的边界。
    """

    def _broken_settings() -> Any:
        raise ConfigurationError("CAMPUS_MCP_COMMAND 不能为空")

    monkeypatch.setattr(client_module, "get_mcp_settings", _broken_settings)
    reset_mcp_client()
    yield
    reset_mcp_client()


class _FakeRuntime:
    """替身运行时：可正常回一轮、可在 ``invoke`` 上抛错，也可携带真实检查点后端。

    - ``checkpointer`` 让用例把「图持有存储器」这一事实交给 ``_close_runtime``
      去取，从而同时观察两条收尾路径；
    - ``on_invoke`` 是「会话期间客户端必须仍然打开」那条用例的探针：它让断言
      发生在**运行过程中**，而不只是收尾之后；
    - ``fail_first`` 用于「第一轮失败、会话继续」的 REPL 场景（只注入一次异常）。
    """

    def __init__(
        self,
        *,
        reply: str = "你好，我是 CampusFlow。",
        checkpointer: Any = None,
        error: BaseException | None = None,
        fail_first: bool = False,
        on_invoke: Any = None,
    ) -> None:
        self.checkpointer = checkpointer
        self.invoke_count = 0
        self._reply = reply
        self._error = error
        self._fail_first = fail_first
        self._on_invoke = on_invoke

    def invoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.invoke_count += 1
        if self._on_invoke is not None:
            self._on_invoke()
        if self._error is not None and (not self._fail_first or self.invoke_count == 1):
            raise self._error
        return {"messages": [AIMessage(content=self._reply)], "intent": "chat"}


def _patch_cli(monkeypatch: pytest.MonkeyPatch, runtime: Any) -> None:
    """把 ``main()`` 的周边依赖换成替身，只观察「收尾是否发生」。

    与 ``tests/test_cli_single_turn.py::_patch_main_environment`` 同一手法：
    配置、日志、追踪、运行时装配全部替身化，而 ``main()`` 的 ``try/finally``
    与真实 ``_close_runtime`` 保持原样——被测的正是那段收尾。
    """

    settings = SimpleNamespace(
        app_name="campus-flow",
        environment="test",
        log_level="INFO",
        llm=SimpleNamespace(provider="fake", model="fake"),
        checkpointer_backend="memory",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_log_startup", lambda settings: None)
    monkeypatch.setattr(cli, "apply_langsmith_tracing", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_build_runtime", lambda settings: runtime)


def _record_close(client: StdioMCPClient) -> list[StdioMCPClient]:
    """把实例的 ``close`` 包一层记录，返回「关闭记录」列表（按调用顺序）。"""

    closes: list[StdioMCPClient] = []
    original_close = client.close

    def _recording_close() -> None:
        closes.append(client)
        original_close()

    client.close = _recording_close  # type: ignore[method-assign]
    return closes


def _feed_inputs(monkeypatch: pytest.MonkeyPatch, inputs: list[str]) -> None:
    """把 ``input()`` 换成按脚本逐条吐出（用完再问即报错，避免悄悄死循环）。"""

    pending: Iterator[str] = iter(inputs)

    def _fake_input(prompt: str = "") -> str:
        try:
            return next(pending)
        except StopIteration:  # pragma: no cover - 说明用例喂的输入不够
            raise AssertionError("交互模式请求了超出脚本的输入，用例输入不足") from None

    monkeypatch.setattr("builtins.input", _fake_input)


def _assert_file_connection_is_closed(saver: Any) -> None:
    """断言 SQLite 检查点连接确实已被关闭（关闭后任何语句都会失败）。"""

    with pytest.raises(sqlite3.ProgrammingError):
        saver.conn.execute("SELECT 1")


# ======================================================= 1 / 2. 正常退出收尾


def test_single_turn_exit_closes_the_mcp_client(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python main.py "问题"`` 正常跑完 → 单例客户端被关闭、缓存被清空。"""

    cached = get_mcp_client()
    closes = _record_close(cached)
    _patch_cli(monkeypatch, _FakeRuntime())

    assert cli.main(["图书馆几点开？"]) == 0

    assert closes == [cached], "单轮退出必须关闭 MCP 客户端（子进程 / 后台线程 / 管道）"
    assert get_mcp_client.cache_info().currsize == 0, "收尾后缓存应清空，下次按最新配置重建"


def test_repl_exit_closes_the_mcp_client(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REPL 输入 ``exit`` 退出 → 同样收尾（长驻入口不留未关闭的 stdio 会话）。"""

    cached = get_mcp_client()
    closes = _record_close(cached)
    _patch_cli(monkeypatch, _FakeRuntime())
    _feed_inputs(monkeypatch, ["你好", "exit"])

    assert cli.main([]) == 0

    assert closes == [cached]


def test_client_stays_open_until_the_session_ends(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """会话进行中客户端必须**保持打开**，收尾只在退出时发生。

    这是「复用 ``reset_mcp_client()`` 是否安全」里最要紧的一条：收尾挂在
    ``main()`` 的 ``finally`` 上，而每一轮都在 ``try`` 之内——若收尾被挪进轮内
    （或 ``_build_runtime`` 装配完就顺手关掉），这里会立刻失败。
    ``reset`` 只清空**缓存**、不碰运行时手里的适配器，因此会话期间不受影响。
    """

    cached = get_mcp_client()
    closes = _record_close(cached)
    seen_close_counts: list[int] = []
    runtime = _FakeRuntime(on_invoke=lambda: seen_close_counts.append(len(closes)))
    _patch_cli(monkeypatch, runtime)
    _feed_inputs(monkeypatch, ["第一轮", "第二轮", "exit"])

    assert cli.main([]) == 0

    assert runtime.invoke_count == 2, "两轮都真的跑了（否则断言的分母不对）"
    assert seen_close_counts == [0, 0], "会话期间不得关闭客户端"
    assert closes == [cached], "退出时恰好关一次"


# ============================================================ 3. 异常退出收尾


def test_run_exception_still_closes_the_mcp_client(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """单轮运行期异常 → 非 0 退出，客户端仍被关闭（异常不跳过收尾）。"""

    cached = get_mcp_client()
    closes = _record_close(cached)
    _patch_cli(monkeypatch, _FakeRuntime(error=RuntimeError("checkpointer 写入失败")))

    assert cli.main(["帮我查一下成绩"]) == 1

    assert closes == [cached]


def test_repl_turn_exception_still_closes_the_mcp_client_on_exit(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """REPL 某一轮失败后继续会话、最终退出 → 客户端仍被关闭（且只关一次）。"""

    cached = get_mcp_client()
    closes = _record_close(cached)
    runtime = _FakeRuntime(error=RuntimeError("本轮炸了"), fail_first=True)
    _patch_cli(monkeypatch, runtime)
    _feed_inputs(monkeypatch, ["会炸的一轮", "正常的一轮", "exit"])

    assert cli.main([]) == 0, "单轮失败不该改变会话的退出码"

    assert runtime.invoke_count == 2
    assert closes == [cached]


# ==================================================== 4. 与检查点后端一同收尾


def test_repl_exit_still_closes_the_checkpointer(
    tmp_path: Any,
    mcp_singleton: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """检查点后端照旧被关闭：新增的 MCP 收尾没有挤掉原有收尾。"""

    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")
    cached = get_mcp_client()
    closes = _record_close(cached)
    _patch_cli(monkeypatch, _FakeRuntime(checkpointer=saver))
    _feed_inputs(monkeypatch, ["你好", "exit"])

    assert cli.main([]) == 0

    assert closes == [cached]
    _assert_file_connection_is_closed(saver)


def test_mcp_close_failure_does_not_skip_the_checkpointer(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """任一处收尾失败，另一处仍执行——``try/finally`` 存在的理由。"""

    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")

    def _explode() -> None:
        raise RuntimeError("MCP 会话关闭失败")

    monkeypatch.setattr(cli, "reset_mcp_client", _explode)

    with pytest.raises(RuntimeError):
        cli._close_runtime(_FakeRuntime(checkpointer=saver))

    _assert_file_connection_is_closed(saver)


# ======================================================== 5. 次数与幂等性


def test_repeated_close_does_not_close_again(mcp_singleton: None) -> None:
    """重复收尾不再关第二次：缓存已清空 → ``reset_mcp_client()`` 直接返回。

    「只会关一次」不是靠调用方小心，而是靠 ``reset`` 的既有语义
    （空缓存即空操作）——这条把它钉住，避免以后有人在收尾里手写
    ``get_mcp_client().close()`` 而引入「关两次 / 关闭不存在的实例」。
    """

    cached = get_mcp_client()
    closes = _record_close(cached)
    runtime = _FakeRuntime()

    cli._close_runtime(runtime)
    cli._close_runtime(runtime)

    assert closes == [cached]


# ======================================================== 6. 没有客户端也安全


def test_shutdown_without_any_mcp_client_is_safe(
    no_mcp_client: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """从未装配过 MCP 客户端（缓存为空）→ 收尾照常，且不会去读「坏掉」的配置。"""

    _patch_cli(monkeypatch, _FakeRuntime())

    assert cli.main(["你好"]) == 0

    assert get_mcp_client.cache_info().currsize == 0


def test_shutdown_is_safe_when_mcp_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``CAMPUS_MCP_ENABLED=false``：真实装配路径**完全不建** MCP 客户端，收尾仍是空操作。

    这条刻意走**真实** ``_build_runtime``（只把 MCP 配置替身为关闭态），
    因此验证的是「关闭态下确实没有客户端被建出来」，而不只是「用例自己没建」。
    """

    monkeypatch.setattr(
        mcp_tools_module, "get_mcp_settings", lambda: make_mcp_settings(enabled=False)
    )
    reset_mcp_client()
    settings = SimpleNamespace(checkpointer_backend="memory", checkpointer_sqlite_path=None)

    try:
        runtime = cli._build_runtime(settings)

        assert get_mcp_client.cache_info().currsize == 0, "MCP 关闭时不该装配客户端"
        cli._close_runtime(runtime)  # 空操作，且不得抛错
    finally:
        reset_mcp_client()


# ============================================================ 7. 收尾的边界


def test_close_does_not_touch_clients_it_does_not_own(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """收尾只作用于进程内单例：手工构造、未被单例持有的客户端不受影响。

    生产路径里客户端**只有** ``get_mcp_client()`` 一个出处，所以这不是「允许泄漏」，
    而是把边界写清楚：收尾不会去猜、不会重复关一个不属于它的实例。
    """

    standalone = StdioMCPClient("python", ("-m", "app.mcp.server"))
    closes = _record_close(standalone)
    _patch_cli(monkeypatch, _FakeRuntime())

    assert cli.main(["你好"]) == 0

    assert closes == []
    assert standalone.started is False


def test_close_of_a_never_started_client_is_still_safe(
    mcp_singleton: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """单例存在但整轮都没走到 MCP（从未 ``start()``）→ 收尾仍然安全且照常执行。"""

    cached = get_mcp_client()  # 只装配、不建连（不拉子进程）
    closes = _record_close(cached)
    _patch_cli(monkeypatch, _FakeRuntime())

    assert cli.main(["你好"]) == 0

    assert closes == [cached]
    assert cached.started is False


# ================================================== 8. 真实 stdio 会话的收尾


def test_real_stdio_session_is_terminated_by_the_close_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实子进程场景：收尾之后 MCP Server 子进程与后台事件循环线程**确实结束**。

    本文件唯一一条真的拉起子进程的用例，手法与 ``tests/mcp/test_client_stdio.py``
    一致（用当前解释器跑 ``-m app.mcp.server``，只做握手与工具发现，不调用业务接口，
    因此不需要 Spring Boot、不联网）。前面十条都用「未建连的客户端」验证接线，
    这一条补上最后一段证据：收尾不是只置了个标志位，而是等后台线程（进而子进程）
    真正退出——也就是 ``StdioMCPClient.close()`` 的语义被生产路径用上了。
    """

    monkeypatch.setattr(
        client_module,
        "get_mcp_settings",
        lambda: make_mcp_settings(
            command=sys.executable, cwd=app_root(), startup_timeout=60.0
        ),
    )
    reset_mcp_client()
    try:
        client = get_mcp_client().start()  # 真实拉起子进程 + 握手 + 工具发现
        background = client._thread

        assert client.started is True
        assert background is not None and background.is_alive(), "前置条件：会话与后台线程都在"

        _patch_cli(monkeypatch, _FakeRuntime())
        assert cli.main(["你好"]) == 0

        assert background.is_alive() is False, "收尾必须等后台事件循环线程收尾"
        assert client.started is False, "会话已关闭"
    finally:
        reset_mcp_client()
