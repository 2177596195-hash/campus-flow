"""CampusFlow 应用启动入口。

仅承担进程级职责：解析启动参数、初始化配置与日志、装配运行时、驱动 Agent。
所有业务能力位于 ``app/`` 包内，本文件不实现任何业务逻辑。
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.agent.graph import KNOWLEDGE_NODE, build_graph, create_checkpointer
from app.agent.graph.checkpoint import CheckpointerError
from app.config.settings import (
    AppSettings,
    ConfigurationError,
    get_rag_settings,
    get_settings,
)
from app.rag import ensure_knowledge_base

logger = logging.getLogger("campus_flow")

# 交互模式下的退出指令
_EXIT_COMMANDS: frozenset[str] = frozenset({"exit", "quit", "/exit", "/quit"})

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """单轮对话的执行结果。"""

    reply: str
    intent: str
    #: 知识库引用来源（仅 knowledge 分支会填充）
    sources: tuple[dict[str, Any], ...] = field(default=())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="campus-flow",
        description="智校园 CampusFlow —— 高校智能事务 Agent",
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="单次提问内容；省略则进入交互模式",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="会话标识（即 LangGraph thread_id），默认自动生成；传入同一值可续用同一会话",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="强制重建知识库（加载 → 清洗 → 切分 → 向量化写入）后退出",
    )
    parser.add_argument("--log-level", default=None, help="日志级别，覆盖环境变量配置")
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format=_LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def _build_runtime(settings: AppSettings) -> CompiledStateGraph:
    """装配 Agent 运行时：图拓扑 + 会话检查点存储器。

    检查点后端来自配置，因此从开发态的内存实现切到生产级持久化方案
    只需调整 ``CHECKPOINTER_BACKEND``，无需改动此处代码。
    RAG 服务由图节点在首次命中 knowledge 意图时惰性装配，
    因此纯对话请求不会初始化 Embedding 与向量库。
    """

    checkpointer = create_checkpointer(settings.checkpointer_backend)
    return build_graph(checkpointer=checkpointer)


def _invoke(runtime: CompiledStateGraph, message: str, thread_id: str) -> TurnResult:
    """在指定会话（thread_id）上执行一轮对话。

    ``thread_id`` 由调用方逐轮传入且保持不变，checkpointer 会据此加载
    该会话的历史状态，因此多轮之间的上下文不依赖任何全局变量。
    """

    state = runtime.invoke(
        {"messages": [HumanMessage(content=message)], "session_id": thread_id},
        config={"configurable": {"thread_id": thread_id}},
    )

    error = state.get("error")
    if error:
        logger.warning("本轮执行存在错误: %s", error)

    final_message = state["messages"][-1]
    content = final_message.content
    reply = content if isinstance(content, str) else str(content)
    intent = str(state.get("intent") or "")
    # 引用来源只对本轮知识问答有意义：checkpointer 会把 sources 留在会话状态里，
    # 若不按 intent 过滤，后续无关轮次会重复展示上一轮的引用。
    sources = tuple(state.get("sources") or ()) if intent == KNOWLEDGE_NODE else ()
    return TurnResult(reply=reply, intent=intent, sources=sources)


def _format_sources(sources: tuple[dict[str, Any], ...]) -> str:
    """把引用来源渲染为终端可读的多行文本。"""

    lines = ["参考来源："]
    for index, item in enumerate(sources, start=1):
        score = item.get("score")
        score_text = f"（相关性 {score}）" if score is not None else ""
        lines.append(f"  [{index}] {item.get('title', '')} — {item.get('source', '')}{score_text}")
    return "\n".join(lines)


def _report(result: TurnResult) -> None:
    print(result.reply)
    if result.sources:
        print(_format_sources(result.sources))


def _run_once(runtime: CompiledStateGraph, message: str, thread_id: str) -> int:
    result = _invoke(runtime, message, thread_id)
    logger.info("单次模式 intent=%s sources=%d", result.intent, len(result.sources))
    _report(result)
    return 0


def _run_repl(runtime: CompiledStateGraph, thread_id: str) -> int:
    """交互模式：整个会话共用一个 thread_id，退出即结束当前会话。"""

    print(f"CampusFlow 交互模式（会话 {thread_id}），输入 exit / quit 退出。")

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in _EXIT_COMMANDS:
            break

        try:
            result = _invoke(runtime, user_input, thread_id)
            print(f"CampusFlow[{result.intent}] > {result.reply}")
            if result.sources:
                print(_format_sources(result.sources))
        except KeyboardInterrupt:
            print("\n已取消本次请求。")
        except Exception:  # noqa: BLE001 - 单轮失败不中断整个会话
            logger.exception("处理用户输入失败")
            print("CampusFlow > 本次请求处理失败，请重试。")

    logger.info("会话结束: %s", thread_id)
    return 0


def _run_ingest() -> int:
    """重建知识库并退出（运维入口，逻辑全部在 app.rag.pipeline）。"""

    try:
        rag_settings = get_rag_settings()
        report = ensure_knowledge_base(settings=rag_settings, rebuild=True)
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 知识库重建失败需明确退出码
        logger.exception("知识库重建失败")
        print(f"[失败] 知识库重建失败: {exc}", file=sys.stderr)
        return 1

    print(report.describe())
    return 0


def _log_startup(settings: AppSettings) -> None:
    logger.info(
        "启动 %s | env=%s | provider=%s | model=%s | checkpointer=%s",
        settings.app_name,
        settings.environment,
        settings.llm.provider,
        settings.llm.model,
        settings.checkpointer_backend,
    )

    # RAG 是可选基础设施：未配置时仅告警，纯对话链路照常可用
    try:
        rag_settings = get_rag_settings()
    except ConfigurationError as exc:
        logger.warning("RAG 未配置，knowledge 意图将不可用: %s", exc)
        return

    logger.info(
        "RAG | vectorstore=%s collection=%s embedding=%s chunk=%d/%d top_k=%d knowledge_dir=%s",
        rag_settings.vectorstore_backend,
        rag_settings.collection_name,
        rag_settings.embedding.model,
        rag_settings.chunk_size,
        rag_settings.chunk_overlap,
        rag_settings.top_k,
        rag_settings.knowledge_dir,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        settings: AppSettings = get_settings()
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    _configure_logging(args.log_level or settings.log_level)
    _log_startup(settings)

    if args.ingest:
        return _run_ingest()

    try:
        runtime = _build_runtime(settings)
    except CheckpointerError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    session_id = args.session_id or uuid.uuid4().hex

    if args.message:
        return _run_once(runtime, args.message, session_id)
    return _run_repl(runtime, session_id)


if __name__ == "__main__":
    raise SystemExit(main())
