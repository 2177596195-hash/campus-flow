"""SQLite 检查点后端测试（同进程，完全离线）。

与 ``test_checkpoint_persistence.py`` 的分工：

- 本文件在同进程内把**装配、配置、生命周期、隔离**逐条钉住（快、纯、细节多）；
- 那份文件用 ``subprocess`` 做**真的换进程**验证（贵，但唯一能证明持久化）。

为什么「持久化」不能只靠本文件：``InMemorySaver`` 在同进程里本来就能恢复状态，
所以这里的
``test_a_new_saver_on_the_same_file_restores_the_task`` 证明的是**状态在文件里
而不在存储器对象里**，真正「跨进程」的结论以另一份文件为准。

覆盖范围：

1. 后端登记表（memory 仍是默认、sqlite 已登记、未登记后端被挡住）；
2. 配置读取（``CHECKPOINTER_BACKEND`` / ``CHECKPOINTER_SQLITE_PATH``，
   含相对路径按应用根目录解析、绝对路径原样使用、默认路径的落点）；
3. 文件与目录（父目录自动创建、状态真的写进配置的那个文件）；
4. 生命周期（关不关连接、能否重复关、memory 与 None 是空操作、
   ``main.py`` 的收尾真的会关掉图里的存储器）；
5. 会话语义（同一文件换一个存储器实例能续上、两个文件互不串、跨 thread 隔离、
   ``pending_value`` 只消费一次且不写回状态）；
6. 架构守卫（检查点层不得反向依赖上层；图定义不得感知具体后端）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

import main as cli
from app.agent.graph import (
    DEFAULT_BACKEND,
    SQLITE_BACKEND,
    CheckpointerError,
    agent_graph,
    build_graph,
    close_checkpointer,
    create_checkpointer,
    supported_backends,
)
from app.agent.graph import checkpoint as checkpoint_module
from app.agent.graph import checkpoint_sqlite as checkpoint_sqlite_module
from app.agent.graph import graph as graph_module
from app.agent.slots import SLOT_SEMESTER, resolve_semester, slot_hint
from app.config import settings as settings_module
from app.config.settings import app_root, get_settings
from tests._fakes import (
    FakeRAGService,
    ScriptedChatModel,
    ScriptedIntentClassifier,
    StaticIntentClassifier,
)

#: 任务上下文提示里的标志串（与 ``tests/agent/test_task_state.py`` 保持一致）
HINT_MARKER: str = "【任务上下文】"

#: 演示场景的两句话
BARE_SCORE_QUESTION: str = "帮我查一下成绩"
SEMESTER_REPLY: str = "上学期"
FOLLOW_UP: str = "再帮我查一下计算机网络"

#: 补齐后的学期（由 slots 解析得到，不写死字面量）
PREVIOUS_SEMESTER: str = str(resolve_semester(SEMESTER_REPLY))

THREAD: str = "thread-sqlite"
OTHER_THREAD: str = "thread-other"

# --------------------------------------------------------------- 测试用构造


def tool_call(name: str, args: dict[str, Any]) -> AIMessage:
    """构造一条「请求调用工具」的 AI 消息（原生 ``tool_calls`` 结构）。"""

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}],
    )


def build_task_graph(
    checkpointer: Any,
    *,
    llm: ScriptedChatModel | None = None,
    classifier: Any = None,
):
    """装配被测图：模型 / 识别器 / RAG 为替身，checkpointer 是**传入的真实实现**。"""

    return build_graph(
        llm=llm or ScriptedChatModel.scripted(AIMessage(content="（脚本未使用）")),
        intent_classifier=classifier or StaticIntentClassifier("query"),
        rag_service=FakeRAGService(),  # type: ignore[arg-type]
        checkpointer=checkpointer,
    )


def invoke(
    graph: Any, text: str, thread_id: str = THREAD
) -> dict[str, Any]:
    return graph.invoke(
        {"messages": [HumanMessage(content=text)], "session_id": thread_id},
        config={"configurable": {"thread_id": thread_id}},
    )


def tool_names(state: dict[str, Any]) -> list[str]:
    return [
        message.name
        for message in state["messages"]
        if isinstance(message, ToolMessage)
    ]


def prompts_with_hint(prompts: list[list[AnyMessage]]) -> list[bool]:
    return [
        any(
            isinstance(message, SystemMessage) and HINT_MARKER in str(message.content)
            for message in prompt
        )
        for prompt in prompts
    ]


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """屏蔽仓库根 ``.env``，配置输入只来自用例本身（与 ``tests/test_config.py`` 同法）。"""

    monkeypatch.setattr(settings_module, "load_environment", lambda: None)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-for-config")
    monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.delenv("CHECKPOINTER_SQLITE_PATH", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ============================================================================
# 1. 后端登记表
# ============================================================================


def test_registry_exposes_both_backends() -> None:
    assert supported_backends() == ("memory", "sqlite")
    assert DEFAULT_BACKEND == "memory"
    assert SQLITE_BACKEND == "sqlite"


def test_default_backend_stays_memory() -> None:
    """默认取值不变：本阶段让持久化成为**选项**，不改变既有默认行为。"""

    assert isinstance(create_checkpointer(), InMemorySaver)
    assert isinstance(create_checkpointer("memory"), InMemorySaver)
    # 后端名大小写 / 空白宽容（沿用既有实现的行为）
    assert isinstance(create_checkpointer("  MEMORY  "), InMemorySaver)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_every_registered_backend_is_buildable(backend: str, tmp_path: Path) -> None:
    """登记表里的每个后端都能真的建出来（新增后端忘了装配会在这里失败）。"""

    saver = create_checkpointer(backend, sqlite_path=tmp_path / "flow.sqlite")
    try:
        # 每个后端都要满足 ``build_graph`` 的契约
        graph = build_task_graph(saver)
        assert graph.checkpointer is saver
    finally:
        close_checkpointer(saver)


def test_unknown_backend_is_rejected_and_lists_options() -> None:
    with pytest.raises(CheckpointerError) as excinfo:
        create_checkpointer("postgres")

    message = str(excinfo.value)
    assert "postgres" in message
    assert "memory" in message
    assert "sqlite" in message


def test_sqlite_backend_requires_a_database_path() -> None:
    """缺路径就报错，而不是悄悄落到某个「猜出来」的位置。"""

    with pytest.raises(CheckpointerError) as excinfo:
        create_checkpointer(SQLITE_BACKEND)

    assert "CHECKPOINTER_SQLITE_PATH" in str(excinfo.value)


def test_memory_backend_ignores_the_sqlite_path(tmp_path: Path) -> None:
    db = tmp_path / "never-created.sqlite"

    saver = create_checkpointer("memory", sqlite_path=db)

    assert isinstance(saver, InMemorySaver)
    assert not db.exists()


def test_backend_selection_is_a_registry_lookup() -> None:
    """后端选择是查表，不是 if/else 堆叠（新增后端只登记一行）。"""

    source = Path(checkpoint_module.__file__).read_text(encoding="utf-8")

    assert "_CHECKPOINTER_FACTORIES.get(name)" in source
    assert "if name ==" not in source
    assert "elif name ==" not in source


# ============================================================================
# 2. 配置读取
# ============================================================================


def test_default_sqlite_path_is_inside_the_app_storage_directory() -> None:
    """默认落点就是验收要求的那一条：``campus-agent/.storage/checkpoints/``。

    这里刻意断言私有常量：验收标准就是这条路径，写成断言比写成注释更难被
    悄悄改掉（也顺手钉住「不放项目根目录」这条要求）。
    """

    default = settings_module._DEFAULT_CHECKPOINT_DB

    assert default.name == "campus_flow.sqlite"
    assert default.parent.name == "checkpoints"
    assert default.parent.parent.name == ".storage"
    assert default.parent.parent.parent == app_root()


def test_backend_and_path_defaults_come_from_config(isolated_settings: None) -> None:
    settings = get_settings()

    assert settings.checkpointer_backend == "memory"
    assert settings.checkpointer_sqlite_path == settings_module._DEFAULT_CHECKPOINT_DB


def test_backend_name_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, isolated_settings: None
) -> None:
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "SQLite")  # 大小写宽容
    get_settings.cache_clear()

    assert get_settings().checkpointer_backend == "sqlite"


def test_relative_sqlite_path_is_resolved_against_the_app_root(
    monkeypatch: pytest.MonkeyPatch, isolated_settings: None
) -> None:
    monkeypatch.setenv("CHECKPOINTER_SQLITE_PATH", "var/checkpoints/flow.sqlite")
    get_settings.cache_clear()

    assert get_settings().checkpointer_sqlite_path == (
        app_root() / "var" / "checkpoints" / "flow.sqlite"
    )


def test_absolute_sqlite_path_is_used_as_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_settings: None
) -> None:
    target = tmp_path / "outside.sqlite"
    monkeypatch.setenv("CHECKPOINTER_SQLITE_PATH", str(target))
    get_settings.cache_clear()

    assert get_settings().checkpointer_sqlite_path == target


# ============================================================================
# 3. 文件与目录
# ============================================================================


def test_sqlite_backend_creates_missing_parent_directories(tmp_path: Path) -> None:
    db = tmp_path / "a" / "b" / "campus_flow.sqlite"
    assert not db.parent.exists()

    saver = create_checkpointer("sqlite", sqlite_path=db)
    try:
        assert db.parent.is_dir()
    finally:
        close_checkpointer(saver)


def test_state_is_written_into_the_configured_file(tmp_path: Path) -> None:
    db = tmp_path / "checkpoints" / "campus_flow.sqlite"
    saver = create_checkpointer("sqlite", sqlite_path=db)
    graph = build_task_graph(saver)

    try:
        state = invoke(graph, BARE_SCORE_QUESTION)

        assert state["pending_slot"] == SLOT_SEMESTER
        assert db.exists()
        # 直接读文件确认状态确实落盘（而不是只在内存里）
        connection = sqlite3.connect(str(db))
        try:
            rows = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        finally:
            connection.close()
        assert rows > 0
    finally:
        close_checkpointer(saver)


# ============================================================================
# 4. 生命周期
# ============================================================================


def test_close_checkpointer_closes_the_file_connection(tmp_path: Path) -> None:
    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")
    assert isinstance(saver, SqliteSaver)

    close_checkpointer(saver)

    with pytest.raises(sqlite3.ProgrammingError):
        saver.conn.execute("SELECT 1")


def test_close_checkpointer_is_a_noop_for_memory_and_for_none() -> None:
    close_checkpointer(create_checkpointer("memory"))
    # 未注入 checkpointer 的模块级图：收尾时取出的是 None，也必须安全
    close_checkpointer(None)


def test_close_checkpointer_is_idempotent(tmp_path: Path) -> None:
    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")

    close_checkpointer(saver)
    close_checkpointer(saver)  # 第二次是空操作，不该抛错


def test_close_runtime_closes_the_checkpointer_of_the_graph(tmp_path: Path) -> None:
    """``main.py`` 的收尾依赖「编译后的图持有传入的存储器」这一点。"""

    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")
    graph = build_task_graph(saver)

    cli._close_runtime(graph)

    with pytest.raises(sqlite3.ProgrammingError):
        saver.conn.execute("SELECT 1")


def test_close_runtime_tolerates_a_runtime_without_a_checkpointer() -> None:
    cli._close_runtime(object())  # type: ignore[arg-type]


def test_compiled_graph_holds_the_injected_checkpointer(tmp_path: Path) -> None:
    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")
    try:
        assert build_task_graph(saver).checkpointer is saver
    finally:
        close_checkpointer(saver)


# ============================================================================
# 5. 会话语义
# ============================================================================


def test_a_new_saver_on_the_same_file_restores_the_task(tmp_path: Path) -> None:
    """换一个存储器实例（模拟重新装配）也能续上：状态在文件里，不在对象里。"""

    db = tmp_path / "flow.sqlite"

    first = create_checkpointer("sqlite", sqlite_path=db)
    try:
        invoke(build_task_graph(first), BARE_SCORE_QUESTION)
    finally:
        close_checkpointer(first)

    second = create_checkpointer("sqlite", sqlite_path=db)
    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"semester": PREVIOUS_SEMESTER}),
        AIMessage(content="上学期成绩已列出。"),
    )
    try:
        restored = invoke(
            build_task_graph(
                second, llm=llm, classifier=StaticIntentClassifier("unknown")
            ),
            SEMESTER_REPLY,
        )
    finally:
        close_checkpointer(second)

    # 孤立识别结果是 unknown，最终意图却是 query：只能来自被恢复的待办
    assert restored["intent"] == "query"
    assert tool_names(restored) == ["get_score"]


def test_two_files_do_not_share_state(tmp_path: Path) -> None:
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"

    saver_a = create_checkpointer("sqlite", sqlite_path=db_a)
    try:
        invoke(build_task_graph(saver_a), BARE_SCORE_QUESTION)  # a 留下待办
    finally:
        close_checkpointer(saver_a)

    saver_b = create_checkpointer("sqlite", sqlite_path=db_b)
    llm_b = ScriptedChatModel.scripted(AIMessage(content="（脚本未使用）"))
    try:
        fresh = invoke(
            build_task_graph(
                saver_b, llm=llm_b, classifier=StaticIntentClassifier("unknown")
            ),
            SEMESTER_REPLY,
        )
    finally:
        close_checkpointer(saver_b)

    assert fresh["intent"] == "unknown"  # b 里没有 a 的待办，孤立结果原样生效
    assert fresh.get("pending_slot") is None
    assert tool_names(fresh) == []
    assert llm_b.calls == 0


def test_threads_are_isolated_inside_one_file(tmp_path: Path) -> None:
    saver = create_checkpointer("sqlite", sqlite_path=tmp_path / "flow.sqlite")
    llm = ScriptedChatModel.scripted(AIMessage(content="（脚本未使用）"))
    graph = build_task_graph(
        saver,
        llm=llm,
        # 第一轮的孤立结果是 query（因此真的会写待办），第二轮是 unknown
        classifier=ScriptedIntentClassifier("query", "unknown"),
    )

    try:
        queued = invoke(graph, BARE_SCORE_QUESTION, THREAD)  # 只写 THREAD 的待办
        other = invoke(graph, SEMESTER_REPLY, OTHER_THREAD)

        assert queued["pending_slot"] == SLOT_SEMESTER
        # 另一个 thread 看不到 THREAD 的待办：孤立结果原样生效，也没花模型调用
        assert other.get("pending_slot") is None
        assert other["intent"] == "unknown"
        assert tool_names(other) == []
        assert llm.calls == 0
    finally:
        close_checkpointer(saver)


def test_pending_value_is_consumed_once_and_never_written_back(tmp_path: Path) -> None:
    db = tmp_path / "flow.sqlite"
    saver = create_checkpointer("sqlite", sqlite_path=db)
    llm = ScriptedChatModel.scripted(
        tool_call("get_score", {"semester": PREVIOUS_SEMESTER}),
        AIMessage(content="上学期成绩已列出。"),
        AIMessage(content="好的。"),
    )
    graph = build_task_graph(
        saver,
        llm=llm,
        # 逐轮孤立识别：第一轮是 query，补槽位那一轮单独看什么都不是
        classifier=ScriptedIntentClassifier("query", "unknown", "query"),
    )

    try:
        invoke(graph, BARE_SCORE_QUESTION)  # 第一轮：写待办，不调模型
        filled = invoke(graph, SEMESTER_REPLY)  # 第二轮：恢复 + 调工具
        after = invoke(graph, FOLLOW_UP)  # 第三轮：全新的一句 query

        # 第二轮两次模型调用：第一次带任务上下文，第二次（工具结果回流后）已用掉；
        # 第三轮完全不该再出现。
        assert prompts_with_hint(llm.received) == [True, False, False]
        assert filled["pending_value"] is None  # 消费完即置空
        assert after.get("pending_value") is None
    finally:
        close_checkpointer(saver)

    # 状态里没有留下 pending_value / pending_slot（补槽位那一轮只清这两个：
    # ``pending_intent`` 会作为「这件事本来是查成绩」的记录留着，只有
    # ``pending_slot`` 非空才代表「正在等用户补槽位」，所以它不影响路由）。
    reopened = create_checkpointer("sqlite", sqlite_path=db)
    try:
        snapshot = build_task_graph(reopened).get_state(
            {"configurable": {"thread_id": THREAD}}
        )
    finally:
        close_checkpointer(reopened)

    assert snapshot.values.get("pending_value") is None
    assert snapshot.values.get("pending_slot") is None


def test_pending_value_hint_is_owned_by_slots() -> None:
    """本文件（与探针）里的标志串必须与真正注入的提示一致，否则断言会静默失效。"""

    assert HINT_MARKER in (slot_hint(PREVIOUS_SEMESTER) or "")


# ============================================================================
# 6. 架构守卫
# ============================================================================


@pytest.mark.parametrize(
    "module", [checkpoint_module, checkpoint_sqlite_module], ids=["checkpoint", "checkpoint_sqlite"]
)
def test_checkpoint_layer_does_not_depend_on_upper_layers(module: Any) -> None:
    """检查点层是基础设施：不得反向依赖图 / 节点 / 工具 / MCP / RAG / HTTP / 配置。"""

    source = Path(module.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "app.agent.nodes",
        "app.agent.graph.graph",
        "app.agent.slots",
        "app.tools",
        "app.mcp",
        "app.rag",
        "app.config",
        "httpx",
    ):
        assert f"import {forbidden}" not in source, f"{module.__name__} 不应 import {forbidden}"


def test_graph_topology_is_unaware_of_the_concrete_backend() -> None:
    """图定义只认 ``BaseCheckpointSaver`` 契约，不认识任何具体后端。"""

    source = Path(graph_module.__file__).read_text(encoding="utf-8")

    assert "checkpoint_sqlite" not in source
    assert "SqliteSaver" not in source
    assert "InMemorySaver" not in source


def test_module_level_graph_has_no_checkpointer() -> None:
    assert getattr(agent_graph, "checkpointer", None) is None
    cli._close_runtime(agent_graph)  # 收尾对它是空操作
