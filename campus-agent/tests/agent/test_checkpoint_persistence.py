"""真实**两进程**持久化测试：进程 A 留下待办 -> 磁盘 -> 进程 B 续上。

与 ``test_checkpoint_sqlite.py`` 的分工：

- 那份在**同一进程内**验证装配、配置、生命周期与隔离（快、纯、覆盖细节）；
- 这份只做一件事，但必须**真的换进程**：用 ``subprocess`` 拉起
  ``tests/agent/_persistence_probe.py``，两次运行之间不存在任何共享对象，
  唯一能携带状态的只有那个 SQLite 文件。

为什么不能退化成「同进程连续两次 invoke」：``InMemorySaver`` 在同进程里本来
就能恢复状态，同进程双跑对「持久化」零信息量。因此本文件的每一条结论都来自
独立的操作系统进程，并且探针会把 ``pid`` 报回来供用例自证。

覆盖的场景就是本阶段验收的那一段：

    进程 A: "帮我查一下成绩"   -> query 意图但没说学期，只追问，一次模型都不花
    （进程退出，状态落在磁盘）
    进程 B: "上学期"           -> 单独看什么都不是（孤立识别 unknown），
                                  却被磁盘上的待办解释成「补学期」-> get_score
                                  -> 拿到该学期真实课程行
    进程 B: 再问一句           -> 任务上下文不再出现（pending_value 只消费一次）
    进程 C: 换 thread_id 说同一句话 -> 什么都继承不到（跨会话隔离）

外部依赖仍是替身（脚本模型 / 脚本意图 / MockTransport 校园业务服务），
但图与 SQLite 检查点存储器都是真实的，因此这里验的是持久化本身，
不是模型质量或网络。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.agent.slots import SLOT_SEMESTER, previous_semester, slot_question

#: 应用根目录（``campus-agent/``）：子进程以它为工作目录与 PYTHONPATH
AGENT_ROOT: Path = Path(__file__).resolve().parents[2]
#: 探针模块名（以 ``-m`` 方式运行，因此子进程能 import ``app`` 与 ``tests``）
PROBE_MODULE: str = "tests.agent._persistence_probe"

#: 演示场景的三句话
BARE_SCORE_QUESTION: str = "帮我查一下成绩"
SEMESTER_REPLY: str = "上学期"
FOLLOW_UP: str = "再帮我查一下计算机网络"

#: 探针替身成绩数据覆盖的当前学期（与 ``DEFAULT_CAMPUS_PAYLOADS`` 对齐）。
#: 「上学期」由 slots 按**配置的当前学期**推导，因此父用例与探针子进程都必须
#: 把日历钉在这同一个值上，解析结果（上一个学期）才会与替身数据一致——
#: 真实日历默认值的变化（学期切换）不影响本用例。
PINNED_CURRENT_SEMESTER: str = "2025-2026-1"

#: 补齐后的学期由 slots 解析得到，而不是在用例里写死一个字面量——
#: 这样「上学期」的语义若随学期推进而变，用例仍然断言正确。
PREVIOUS_SEMESTER: str = previous_semester(PINNED_CURRENT_SEMESTER) or ""

THREAD: str = "persist-thread"
OTHER_THREAD: str = "fresh-thread"


def _probe_env() -> dict[str, str]:
    """子进程环境：把应用根挂上 PYTHONPATH，并确保不上报 trace。

    同时把教务日历钉在 ``PINNED_CURRENT_SEMESTER``：子进程里「上学期」的
    解析与父用例的 ``PREVIOUS_SEMESTER`` 来自同一份配置口径。
    """

    env = dict(os.environ)
    env["PYTHONPATH"] = str(AGENT_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["LANGSMITH_TRACING"] = "false"
    env["CAMPUS_CURRENT_SEMESTER"] = PINNED_CURRENT_SEMESTER
    return env


def run_probe(
    *,
    db: Path,
    thread: str,
    intents: list[str],
    texts: list[str],
    tmp_path: Path,
    tag: str,
) -> dict[str, Any]:
    """在**独立进程**里跑若干轮对话，返回探针观测结果。

    Raises:
        AssertionError: 子进程非零退出（把 stderr 一并带出来，避免只看到 assert 失败）。
    """

    out = tmp_path / f"probe-{tag}.json"
    command = [
        sys.executable,
        "-m",
        PROBE_MODULE,
        "--db",
        str(db),
        "--out",
        str(out),
        "--thread",
        thread,
        "--semester",
        PREVIOUS_SEMESTER,
    ]
    for intent in intents:
        command += ["--intent", intent]
    command += texts

    completed = subprocess.run(
        command,
        cwd=str(AGENT_ROOT),
        env=_probe_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, (
        f"探针进程失败（exit={completed.returncode}）\n"
        f"command: {' '.join(command)}\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    return json.loads(out.read_text(encoding="utf-8"))


def checkpoint_threads(db: Path) -> set[str]:
    """直接读 SQLite 文件，列出其中出现过的 ``thread_id``。"""

    connection = sqlite3.connect(str(db))
    try:
        rows = connection.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


# ============================================================================
# 1. 进程 A 留下待办 -> 进程 B（新进程）续上
# ============================================================================


def test_process_two_recovers_the_pending_task_from_the_database(tmp_path: Path) -> None:
    db = tmp_path / "checkpoints" / "campus_flow.sqlite"  # 父目录刻意不存在

    # ---- 进程 A：只在**这一轮**里知道的待办，轮末必须已经落盘 ----
    first = run_probe(
        db=db,
        thread=THREAD,
        intents=["query"],
        texts=[BARE_SCORE_QUESTION],
        tmp_path=tmp_path,
        tag="p1",
    )
    asked = first["turns"][0]

    assert asked["intent"] == "query"
    assert asked["pending_slot"] == SLOT_SEMESTER
    assert asked["pending_intent"] == "query"
    assert asked["pending_value"] is None
    assert asked["tool_names"] == []  # 缺槽位时一次工具都不执行
    assert asked["model_calls"] == 0  # 追问是确定性的，不花模型调用
    assert asked["reply"] == slot_question(SLOT_SEMESTER)
    # 数据库文件（连同两层父目录）由程序自己建出来，不要求先手工建目录
    assert db.exists(), "SQLite 数据库文件应当被自动创建"

    # ---- 进程 B：另一个操作系统进程，除磁盘上的文件外没有任何共享 ----
    second = run_probe(
        db=db,
        thread=THREAD,
        intents=["unknown", "query"],  # 「上学期」单独看确实什么都不是
        texts=[SEMESTER_REPLY, FOLLOW_UP],
        tmp_path=tmp_path,
        tag="p2",
    )
    assert first["pid"] != second["pid"], "两次运行必须真的是两个进程"
    restored, after = second["turns"]

    # 孤立识别结果是 unknown，最终意图却是 query：只剩「待办被恢复」一种解释
    assert restored["intent"] == "query"
    # 补槽位那一轮只清 ``pending_slot`` / ``pending_value``——``pending_intent``
    # 是「这件事本来是查成绩」的记录，留着不影响路由：只有 ``pending_slot``
    # 非空才代表「正在等用户补槽位」。
    assert restored["pending_slot"] is None
    assert restored["pending_value"] is None  # 消费完即置空，且本来就不写回状态
    assert restored["tool_calls"] == ["get_score"]
    assert restored["tool_names"] == ["get_score"]
    # 工具真的被调用并拿到了「该学期」的数据：semester 传下去了，服务端按它过滤
    assert PREVIOUS_SEMESTER in restored["tool_contents"][0]
    assert "高等数学" in restored["tool_contents"][0]

    # 任务上下文只在「补槽位那一轮」的第一次模型调用里出现：
    #   第一次（决策是否调工具）带上了它，第二次（工具结果回流后汇总）已经用掉了
    assert restored["hint_seen"] == [True, False]
    # 下一轮是全新的一句 query，不该再看到上一轮的任务上下文
    assert after["hint_seen"] == [False]
    assert after["tool_names"] == []

    # 磁盘上确实留下了这条会话的记录
    assert THREAD in checkpoint_threads(db)


# ============================================================================
# 2. 换一个 thread_id：新进程里什么也继承不到
# ============================================================================


def test_a_different_thread_in_a_new_process_inherits_nothing(tmp_path: Path) -> None:
    db = tmp_path / "campus_flow.sqlite"

    run_probe(
        db=db,
        thread=THREAD,
        intents=["query"],
        texts=[BARE_SCORE_QUESTION],
        tmp_path=tmp_path,
        tag="p1",
    )

    other = run_probe(
        db=db,
        thread=OTHER_THREAD,
        intents=["unknown"],
        texts=[SEMESTER_REPLY],
        tmp_path=tmp_path,
        tag="p2",
    )
    turn = other["turns"][0]

    # 同一个数据库、同一句话：没有待办可补，孤立识别结果原样生效
    assert turn["intent"] == "unknown"
    assert turn["pending_slot"] is None
    assert turn["pending_value"] is None
    assert turn["tool_calls"] == []
    assert turn["model_calls"] == 0  # 落到了占位分支，连模型都没调
    # 两个会话都在文件里，但互不干涉
    assert checkpoint_threads(db) == {THREAD, OTHER_THREAD}
