"""单轮 CLI（``--single``，即 ``main.py "问题"``）异常兜底的行为测试（完全离线）。

这里验证一件事：**节点之外的意外不会以 traceback 的形式糊到用户脸上**，
同时也不会被静默吞掉。

分工要说清楚，否则容易把「兜底」写错层次：

===================================  ==========================================
失败发生在哪儿                        由谁负责转成友好回复
===================================  ==========================================
节点内部（业务可预期）                节点自己写 ``state["error"]`` + 友好回复，
                                    ``_invoke`` 只记一条 warning
``invoke`` 外层（意外）               ``_run_once`` / ``_run_repl`` 的最外层兜底
                                    = 完整堆栈进日志 + 一句提示（本文件）
===================================  ==========================================

因此本文件的用例刻意**不去构造节点**，而是让替身运行时在 ``invoke`` 上抛错——
那正是「能冒到最外层」的那一类异常（checkpointer 写入失败、运行时配置漂移等）。

不启真实 Spring Boot，不拉 MCP 子进程，不连 LangSmith。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import main as cli
from app.agent.graph.checkpoint import CheckpointerError

#: 与两条入口共用的那句提示对齐（写入用户可见输出的契约；改一处必须改两处，
#: ``test_both_entrypoints_report_the_same_failure_hint`` 会盯着这件事）。
TURN_FAILURE_HINT = "本次请求处理失败，请重试。"

#: 用户主动中断时的提示（交互模式既有文案，单轮模式沿用）。
CANCEL_HINT = "已取消本次请求。"

#: 故意带「内部细节」味道的异常消息：它只允许出现在日志里，不允许出现在终端输出里。
LEAKY_DETAIL = "SQLITE_IOERR: /var/lib/secret/checkpoints.sqlite 写入失败"


class _ReplyRuntime:
    """正常返回一轮结果的替身运行时。"""

    def __init__(self, reply: str = "你好，我是 CampusFlow。", intent: str = "chat") -> None:
        self._reply = reply
        self._intent = intent
        self.calls: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls.append(payload)
        return {"messages": [AIMessage(content=self._reply)], "intent": self._intent}


class _ExplodingRuntime:
    """在 ``invoke`` 外层抛错的替身运行时（模拟 checkpointer / 运行时故障）。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def invoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls += 1
        raise self._error


class _FlakyRuntime:
    """第一轮抛错、之后正常：用于确认交互模式「单轮失败不中断会话」。"""

    def __init__(self, error: BaseException, reply: str = "第二轮正常。") -> None:
        self._error = error
        self._reply = reply
        self.calls = 0

    def invoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            raise self._error
        return {"messages": [AIMessage(content=self._reply)], "intent": "chat"}


def _feed_inputs(monkeypatch: pytest.MonkeyPatch, inputs: list[str]) -> None:
    """把 ``input()`` 换成按脚本逐条吐出（用完再问即报错，避免悄悄死循环）。"""

    pending: Iterator[str] = iter(inputs)

    def _fake_input(prompt: str = "") -> str:
        try:
            return next(pending)
        except StopIteration:  # pragma: no cover - 说明用例喂的输入不够
            raise AssertionError("交互模式请求了超出脚本的输入，用例输入不足") from None

    monkeypatch.setattr("builtins.input", _fake_input)


# ============================================================ 1. 正常路径不变


def test_run_once_returns_zero_and_prints_the_reply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """正常一轮：退出码 0、回复照常输出——兜底没有改变既有行为。"""

    runtime = _ReplyRuntime(reply="图书馆 8:00 开放。", intent="knowledge")

    assert cli._run_once(runtime, "图书馆几点开？", "sess-ok") == 0

    out = capsys.readouterr()
    assert "图书馆 8:00 开放。" in out.out
    assert runtime.calls[0]["session_id"] == "sess-ok"


# ====================================================== 2. 单轮外层异常兜底


def test_run_once_returns_nonzero_on_an_outer_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """外层异常不再直接崩：退出码非 0，且给出与交互模式同一句提示。"""

    runtime = _ExplodingRuntime(RuntimeError(LEAKY_DETAIL))

    assert cli._run_once(runtime, "帮我查一下成绩", "sess-boom") == 1

    out = capsys.readouterr()
    assert TURN_FAILURE_HINT in out.err
    assert runtime.calls == 1, "失败发生在 invoke 上，说明异常确实来自最外层"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(CheckpointerError("sqlite 检查点后端不可用"), id="checkpointer"),
        pytest.param(sqlite3.OperationalError("database is locked"), id="sqlite"),
        pytest.param(OSError("管道已断开"), id="os-error"),
    ],
)
def test_run_once_swallows_every_exception_family(error: Exception) -> None:
    """不同家族的意外一视同仁：都转成非 0 退出码，都不会逃逸到调用方。"""

    assert cli._run_once(_ExplodingRuntime(error), "你好", "sess-family") == 1


def test_run_once_never_leaks_internal_detail_to_the_user(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """用户看得到的只有一句提示：无 traceback、无异常类名、无内部路径。"""

    cli._run_once(_ExplodingRuntime(RuntimeError(LEAKY_DETAIL)), "你好", "sess-leak")

    out = capsys.readouterr()
    visible = out.out + out.err
    assert TURN_FAILURE_HINT in visible
    assert "Traceback" not in visible
    assert "RuntimeError" not in visible
    assert LEAKY_DETAIL not in visible


def test_run_once_logs_the_full_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """堆栈不给孩子看，但必须完整进日志——否则线上无从定位。"""

    caplog.set_level(logging.ERROR, logger="campus_flow")

    cli._run_once(_ExplodingRuntime(RuntimeError(LEAKY_DETAIL)), "你好", "sess-log")

    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(records) == 1
    assert records[0].exc_info is not None, "必须带 exc_info，才能留下完整堆栈"
    assert "Traceback (most recent call last)" in caplog.text
    assert LEAKY_DETAIL in caplog.text


def test_run_once_reports_a_user_cancel_without_logging_a_traceback(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C 是用户主动取消，不是故障：给取消提示，且不往日志里灌堆栈。"""

    caplog.set_level(logging.ERROR, logger="campus_flow")

    assert cli._run_once(_ExplodingRuntime(KeyboardInterrupt()), "你好", "sess-ctrl-c") == 1

    out = capsys.readouterr()
    assert CANCEL_HINT in out.out
    assert TURN_FAILURE_HINT not in out.out + out.err
    assert caplog.records == []


# ================================================== 3. main() 集成接线（含收尾）


def test_main_returns_nonzero_and_still_closes_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``python main.py "问题"`` 整体：非 0 退出，且 ``finally`` 里的收尾仍执行。

    收尾这一点很关键：异常兜底若写错位置（例如吞掉后让 ``finally`` 失效），
    SQLite 检查点连接就会漏掉，这正是 main.py 里那段 ``finally`` 存在的理由。
    """

    closed: list[Any] = []
    runtime = _ExplodingRuntime(RuntimeError(LEAKY_DETAIL))
    _patch_main_environment(monkeypatch, runtime=runtime, on_close=closed.append)

    assert cli.main(["帮我查一下成绩"]) == 1

    out = capsys.readouterr()
    assert TURN_FAILURE_HINT in out.err
    assert closed == [runtime], "运行时收尾必须照常执行"


def _patch_main_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: Any,
    on_close: Any,
) -> None:
    """把 ``main()`` 的周边依赖换成替身，只观察「异常如何冒泡 + 是否收尾」。

    与 ``tests/rag/test_langsmith_dataset.py::_patch_main_for_dispatch`` 同一手法：
    配置、日志、追踪、运行时装配、收尾全部替身化，用例不读环境变量、不连外部服务。
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
    monkeypatch.setattr(cli, "_close_runtime", on_close)


# ======================================== 4. 交互模式既有行为未被破坏（回归）


def test_repl_still_recovers_from_a_failed_turn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """交互模式照旧：某一轮失败只是那一轮的事，会话继续，退出码仍为 0。"""

    runtime = _FlakyRuntime(RuntimeError(LEAKY_DETAIL), reply="第二轮正常。")
    _feed_inputs(monkeypatch, ["第一轮会炸", "第二轮正常", "exit"])

    assert cli._run_repl(runtime, "sess-repl") == 0

    out = capsys.readouterr()
    assert TURN_FAILURE_HINT in out.out
    assert "第二轮正常。" in out.out
    assert runtime.calls == 2, "失败没有让会话提前结束"


def test_repl_still_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """交互模式对 Ctrl+C 的既有提示与「不中断会话」语义保持不变。"""

    runtime = _FlakyRuntime(KeyboardInterrupt(), reply="打断后的下一轮。")
    _feed_inputs(monkeypatch, ["会被打断", "再来一次", "exit"])

    assert cli._run_repl(runtime, "sess-repl-ctrl-c") == 0

    out = capsys.readouterr()
    assert CANCEL_HINT in out.out
    assert "打断后的下一轮。" in out.out


# ==================================================== 5. 两条入口的提示一致性


def test_both_entrypoints_report_the_same_failure_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """单轮与交互看到的是同一句话——否则「保持一致的用户体验」就只是口号。"""

    cli._run_once(_ExplodingRuntime(RuntimeError("boom")), "你好", "sess-same-1")
    single = capsys.readouterr()

    _feed_inputs(monkeypatch, ["会炸", "exit"])
    cli._run_repl(_FlakyRuntime(RuntimeError("boom")), "sess-same-2")
    repl = capsys.readouterr()

    assert TURN_FAILURE_HINT in single.err
    assert TURN_FAILURE_HINT in repl.out
