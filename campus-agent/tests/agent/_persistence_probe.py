"""跨进程持久化探针：由 ``test_checkpoint_persistence.py`` 以**子进程**拉起。

为什么必须是子进程：``InMemorySaver`` 在同进程里本来就能恢复状态，所以
「同进程连续两次 invoke 还能续上」对「持久化」零信息量。只有**换一个操作系统
进程**、两个进程之间不存在任何共享对象，还能用同一个 ``thread_id`` 取回上一轮
留下的待办任务，才说明状态真的落在了磁盘上。

本文件刻意不叫 ``test_*.py``：它不是用例，而是用例的「被测进程」。

调用约定::

    python -m tests.agent._persistence_probe \\
        --db <SQLite 文件> --out <结果 JSON> \\
        --thread <thread_id> --semester <补齐后应传给工具的学期> \\
        --intent <该轮孤立识别结果（可重复）> \\
        <该轮用户输入（可多个）>

结果 JSON::

    {"pid": ..., "thread": ..., "turns": [{
        "text", "intent", "pending_slot", "pending_intent", "pending_value",
        "tool_calls", "tool_names", "tool_contents", "reply",
        "model_calls", "hint_seen"
    }, ...]}

两个刻意加进来的观测点：

- ``pid``：让用例能自证「这确实是两个进程」，而不是同进程跑了两遍；
- ``hint_seen``：本轮**每次**模型调用是否收到那句任务上下文
  （``app/agent/slots.slot_hint``），它是「``pending_value`` 只被消费一次」
  的唯一观测点——只断言轮末状态的话，「消费了」与「压根没注入」长得一样。

外部依赖全部是替身：模型用 ``ScriptedChatModel``、意图用
``ScriptedIntentClassifier``、校园业务服务用挂在 ``httpx.MockTransport``
上的 ``make_campus_client``。因此本探针不联网、不调真实模型、不需要
Spring Boot，但走的仍是**真实**的图与真实的 SQLite 存储器。
"""

from __future__ import annotations

import os

# 必须在导入 LangChain / LangGraph 之前：子进程独立环境，绝不把 trace 上报到线上。
# （pytest 会话夹具虽然也会带上关闭值，但探针不该依赖父进程的环境是否干净。）
os.environ["LANGSMITH_TRACING"] = "false"

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agent.graph import build_graph, close_checkpointer, create_checkpointer  # noqa: E402
from app.tools import score_tools  # noqa: E402
from tests._fakes import (  # noqa: E402
    DEFAULT_CAMPUS_PAYLOADS,
    FakeRAGService,
    ScriptedChatModel,
    ScriptedIntentClassifier,
    make_campus_client,
)

#: 任务上下文提示里的标志串（与 ``tests/agent/test_task_state.py`` 保持一致）
HINT_MARKER: str = "【任务上下文】"

#: 补齐学期后模型给出的最终回答（内容无关紧要，本探针断言的是工具真的被调用了）
FINAL_ANSWER: str = "上学期成绩已列出。"
#: 补槽位之后的下一轮回答（不含 tool_calls，用来观察是否重复注入任务上下文）
FOLLOW_UP_ANSWER: str = "好的。"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跨进程检查点持久化探针")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    parser.add_argument("--out", required=True, help="结果 JSON 输出路径")
    parser.add_argument("--thread", required=True, help="thread_id（会话标识）")
    parser.add_argument(
        "--semester",
        required=True,
        help="补齐槽位后应当传给 get_score 的学期（由用例侧从 slots 解析得到）",
    )
    parser.add_argument(
        "--intent",
        action="append",
        required=True,
        help="逐轮孤立识别结果，可重复；用尽后重复最后一个（与 ScriptedIntentClassifier 一致）",
    )
    parser.add_argument("texts", nargs="+", help="逐轮用户输入")
    return parser.parse_args(argv)


def _tool_call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}],
    )


def _this_turn_messages(state: dict[str, Any]) -> list[Any]:
    """只取**本轮**产生的消息（最后一条用户消息之后的部分）。

    checkpointer 会把同一会话的历史一并带回，不截断的话上一轮的工具消息会被
    重复计入本轮的断言。
    """

    messages = list(state["messages"])
    last_user = max(
        index
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage)
    )
    return messages[last_user + 1 :]


def _has_hint(prompt: list[Any]) -> bool:
    return any(
        isinstance(message, SystemMessage) and HINT_MARKER in str(message.content)
        for message in prompt
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # 校园业务服务替身：按 ``semester`` 查询参数过滤，与 Spring Boot 一致。
    # 工具内部读的是模块全局名 ``get_campus_api_client``，替换它即可生效。
    score_tools.get_campus_api_client = lambda: make_campus_client(  # type: ignore[assignment]
        payloads=DEFAULT_CAMPUS_PAYLOADS
    )

    llm = ScriptedChatModel.scripted(
        _tool_call("get_score", {"semester": args.semester}),
        AIMessage(content=FINAL_ANSWER),
        AIMessage(content=FOLLOW_UP_ANSWER),
    )
    classifier = ScriptedIntentClassifier(*args.intent)

    graph = build_graph(
        llm=llm,
        intent_classifier=classifier,
        rag_service=FakeRAGService(),  # type: ignore[arg-type]
        checkpointer=create_checkpointer("sqlite", sqlite_path=Path(args.db)),
    )

    config = {"configurable": {"thread_id": args.thread}}
    turns: list[dict[str, Any]] = []
    try:
        for text in args.texts:
            before = llm.calls
            state = graph.invoke(
                {"messages": [HumanMessage(content=text)], "session_id": args.thread},
                config=config,
            )
            turn_messages = _this_turn_messages(state)
            turns.append(
                {
                    "text": text,
                    "intent": state.get("intent"),
                    "pending_slot": state.get("pending_slot"),
                    "pending_intent": state.get("pending_intent"),
                    "pending_value": state.get("pending_value"),
                    "tool_calls": [
                        call["name"]
                        for message in turn_messages
                        if isinstance(message, AIMessage)
                        for call in message.tool_calls
                    ],
                    "tool_names": [
                        message.name
                        for message in turn_messages
                        if isinstance(message, ToolMessage)
                    ],
                    "tool_contents": [
                        str(message.content)
                        for message in turn_messages
                        if isinstance(message, ToolMessage)
                    ],
                    "reply": str(state["messages"][-1].content),
                    "model_calls": llm.calls - before,
                    "hint_seen": [_has_hint(prompt) for prompt in llm.received[before:]],
                }
            )
    finally:
        # 与 main.py 的收尾同一条规矩：进程结束前显式关掉后端连接
        close_checkpointer(getattr(graph, "checkpointer", None))

    Path(args.out).write_text(
        json.dumps(
            {"pid": os.getpid(), "thread": args.thread, "turns": turns},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
