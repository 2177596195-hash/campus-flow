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

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.agent.graph import KNOWLEDGE_NODE, build_graph, close_checkpointer, create_checkpointer
from app.agent.graph.checkpoint import CheckpointerError
from app.agent.model import build_llm
from app.agent.nodes import tool_call_names
from app.agent.run_context import (
    ENTRYPOINT_CLI_REPL,
    ENTRYPOINT_CLI_SINGLE,
    build_run_context,
)
from app.config.settings import (
    AppSettings,
    ConfigurationError,
    apply_langsmith_tracing,
    get_campus_api_settings,
    get_mcp_settings,
    get_rag_settings,
    get_settings,
)
from app.mcp.client import reset_mcp_client
from app.mcp.tools import build_agent_tools
from app.rag import ingest_directory, resolve_manifest_path
from app.rag.service import build_rag_service
from app.rag.evaluation import (
    DatasetError,
    default_retriever,
    evaluate_dataset,
    load_dataset,
    summarize_dataset,
)
# 回答相关性 Judge 与它的评估模型都在这里注入：``answer_judge`` 模块本身不读配置、
# 不 import 模型工厂，因此「用哪个模型判定」由组合根（本文件）决定，
# 评估层「不越过 Retriever 拿模型」的边界保持不变。
from app.rag.evaluation.answer_judge import JUDGE_TEMPERATURE, AnswerRelevanceJudge
# LangSmith 相关的两个模块都直接按模块导入（不经过包的 __init__）：架构守卫只对
# 这两个文件开豁免，让包 __init__ 保持「与 LangSmith 无关」，普通运行路径也就
# 不会带进任何远端概念。
from app.rag.evaluation.langsmith_dataset import (
    DEFAULT_DATASET_NAME,
    DatasetSyncError,
    build_dataset_client,
    sync_rag_dataset,
)
from app.rag.evaluation.langsmith_evaluators import (
    FULL_SELECTION,
    SMOKE_SELECTION,
    build_answer_relevance_evaluator,
    build_answer_target,
    build_retrieval_target,
    run_answer_relevance_evaluation,
    run_retrieval_evaluation,
)

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
    #: 本轮模型请求调用的工具名（仅 Tool Calling 分支会填充），用于展示调用链
    tool_calls: tuple[str, ...] = field(default=())


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
        help="对知识库执行增量索引（NEW/CHANGED/DELETED 对齐）后退出",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="与 --ingest 联用：清空向量库后全量重建",
    )
    parser.add_argument(
        "--evaluate-rag",
        action="store_true",
        help="对内置评估数据集执行检索评估（Hit@1 / Hit@k / Precision / Recall）后退出",
    )
    parser.add_argument(
        "--sync-rag-dataset",
        action="store_true",
        help="把内置评估数据集同步到 LangSmith Dataset（幂等：已存在则复用）后退出",
    )
    parser.add_argument(
        "--evaluate-rag-langsmith",
        action="store_true",
        help="在 LangSmith 上执行一次检索评估（默认只跑最小样本：1 正 + 1 负）后退出",
    )
    parser.add_argument(
        "--evaluate-rag-answer",
        action="store_true",
        help=(
            "在 LangSmith 上执行一次回答相关性评估"
            "（LLM-as-a-Judge，默认只跑数据集里的 2 条真实用例）后退出"
        ),
    )
    parser.add_argument(
        "--langsmith-full",
        action="store_true",
        help="与 --evaluate-rag-langsmith 联用：跑数据集全部用例（默认只跑最小样本）",
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
    """装配 Agent 运行时：工具实现 + 图拓扑 + 会话检查点存储器。

    检查点后端来自配置，因此从开发态的内存实现切到文件级持久化
    （进程重启后同一 ``thread_id`` 仍能取回会话状态）只需调整
    ``CHECKPOINTER_BACKEND``，无需改动此处代码。
    ``sqlite_path`` 一律从配置传入（其余后端忽略它）：后端不自己去找默认路径，
    于是「会话状态落在哪个文件」这件事**只有配置一个出处**。

    RAG 服务由图节点在首次命中 knowledge 意图时惰性装配，
    因此纯对话请求不会初始化 Embedding 与向量库。

    工具清单在**这里**解析：``CAMPUS_MCP_ENABLED`` 决定考试查询是走
    Campus MCP Server 还是直连业务服务。放在装配点是刻意的——
    图定义（``graph.py``）保持「只看拓扑」，不读配置、不拉子进程，
    模块导入与测试导入图时都不会产生副作用。
    """

    tools = build_agent_tools()
    checkpointer = create_checkpointer(
        settings.checkpointer_backend,
        sqlite_path=settings.checkpointer_sqlite_path,
    )
    return build_graph(checkpointer=checkpointer, tools=tools)


def _close_runtime(runtime: CompiledStateGraph) -> None:
    """释放运行时占用的进程级资源：MCP 会话 + 检查点后端。

    编译后的图**持有**注入的 checkpointer（``CompiledStateGraph.checkpointer``），
    所以后端资源的持有者就是运行时本身：收尾时从它取出存储器关闭即可，
    不必在 main.py 里另存一份引用。``memory`` 后端没有外部资源，是空操作；
    REPL 这类长驻入口则因此不会留下一条永不关闭的 SQLite 连接。

    MCP 客户端是**另一类**资源，而且它不挂在图上：生产运行时由装配期的
    ``build_agent_tools()`` 按配置从进程内单例取用（``get_mcp_client()``），
    首次工具发现时拉起一个 MCP Server 子进程，并由此独占一条后台事件循环线程
    与一对 stdio 管道。这三样都不该交给「daemon 线程 + 进程退出」去间接回收——
    那是「反正会没」，不是收尾。关闭动作直接复用 ``reset_mcp_client()``：
    它本就是「关闭缓存实例 + 清空缓存」，与收尾要做的两件事逐字一致，
    因此不在 MCP 层再造一个只有名字不同的入口。

    两处收尾互不依赖，但用 ``try/finally`` 串起来：任一处失败都不会让另一处漏关。
    未装配过客户端时 ``reset_mcp_client()`` 是空操作（缓存为空即直接返回），
    因此 ``CAMPUS_MCP_ENABLED=false``、或整轮都没走到 MCP 时，收尾照常安全。
    """

    try:
        # 先收 MCP 会话（子进程 / 后台循环线程 / stdio 管道），再关检查点连接。
        reset_mcp_client()
    finally:
        close_checkpointer(getattr(runtime, "checkpointer", None))


def _turn_tool_calls(state: dict[str, Any]) -> tuple[str, ...]:
    """取出**本轮**请求调用的工具名。

    checkpointer 会把同一会话的历史消息一并带回，因此必须先定位本轮起点
    （最后一条用户消息），否则上一轮的工具调用会被重复展示。
    与 sources 的处理同理：展示层只关心当前这一轮。
    """

    messages: list[AnyMessage] = list(state.get("messages") or [])
    last_user_index = max(
        (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=-1,
    )
    return tool_call_names(messages[last_user_index + 1 :])


def _tracing_user_id() -> str:
    """本次调用的用户标识。

    当前取自校园业务配置里的学号占位（`CAMPUS_STUDENT_ID` 的既有定位就是
    「接入登录态前的身份占位」）。未配置时返回空串，由 ``build_run_context``
    省略该字段——观测信息不该让主流程失败。
    """

    try:
        return get_campus_api_settings().default_student_id
    except ConfigurationError:
        return ""


def _invoke(
    runtime: CompiledStateGraph,
    message: str,
    thread_id: str,
    entrypoint: str = ENTRYPOINT_CLI_SINGLE,
) -> TurnResult:
    """在指定会话（thread_id）上执行一轮对话。

    ``thread_id`` 由调用方逐轮传入且保持不变，checkpointer 会据此加载
    该会话的历史状态，因此多轮之间的上下文不依赖任何全局变量。

    ``config`` 全部由 ``build_run_context`` 用 LangGraph 原生字段装配
    （run_name / tags / metadata / configurable），因此这次调用在追踪平台里
    能直接看出属于哪个项目、哪个会话、哪个入口。意图不在这里给出——它要等
    router 跑完才知道，由 router 追加为 tag（见 ``app.agent.run_context``）。
    """

    config = build_run_context(
        session_id=thread_id,
        user_id=_tracing_user_id(),
        entrypoint=entrypoint,
    )
    state = runtime.invoke(
        {"messages": [HumanMessage(content=message)], "session_id": thread_id},
        config=config,
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
    return TurnResult(
        reply=reply,
        intent=intent,
        sources=sources,
        tool_calls=_turn_tool_calls(state),
    )


def _format_sources(sources: tuple[dict[str, Any], ...]) -> str:
    """把引用来源渲染为终端可读的多行文本。"""

    lines = ["参考来源："]
    for index, item in enumerate(sources, start=1):
        score = item.get("score")
        score_text = f"（相关性 {score}）" if score is not None else ""
        lines.append(f"  [{index}] {item.get('title', '')} — {item.get('source', '')}{score_text}")
    return "\n".join(lines)


def _format_tool_calls(names: tuple[str, ...]) -> str:
    """把本轮工具调用渲染为终端可读的一行文本。"""

    return "调用工具：" + "、".join(names)


def _report(result: TurnResult) -> None:
    if result.tool_calls:
        print(_format_tool_calls(result.tool_calls))
    print(result.reply)
    if result.sources:
        print(_format_sources(result.sources))


def _run_once(runtime: CompiledStateGraph, message: str, thread_id: str) -> int:
    """单次模式：跑一轮问答后退出。

    异常兜底刻意放在**这一层**，与 ``_run_repl`` 的每轮兜底同语义：节点内部
    已把可预期的业务失败转成友好回复（并写进 ``state["error"]``，见 ``_invoke``），
    能冒到这里的是**节点之外**的意外——checkpointer 写入失败、``invoke`` 外层
    异常、运行时配置漂移等。它们不该以 Python traceback 的形式糊到用户脸上，
    也不该被静默吞掉：完整堆栈进日志，用户只看到与交互模式一致的一句提示，
    同时以非 0 退出码告诉脚本调用方「这轮没成功」。

    不在节点里再补 ``try/except``——那会让「哪些失败算业务失败」散落到各处。
    """

    try:
        result = _invoke(runtime, message, thread_id, ENTRYPOINT_CLI_SINGLE)
    except KeyboardInterrupt:  # 用户主动中断：与交互模式同样的提示，只是无会话可续
        print("\n已取消本次请求。")
        return 1
    except Exception:  # noqa: BLE001 - 最外层兜底：单轮失败不得抛出 traceback
        logger.exception("单次请求处理失败")
        print("CampusFlow > 本次请求处理失败，请重试。", file=sys.stderr)
        return 1

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
            result = _invoke(runtime, user_input, thread_id, ENTRYPOINT_CLI_REPL)
            print(f"CampusFlow[{result.intent}] > {result.reply}")
            if result.tool_calls:
                print(_format_tool_calls(result.tool_calls))
            if result.sources:
                print(_format_sources(result.sources))
        except KeyboardInterrupt:
            print("\n已取消本次请求。")
        except Exception:  # noqa: BLE001 - 单轮失败不中断整个会话
            logger.exception("处理用户输入失败")
            print("CampusFlow > 本次请求处理失败，请重试。")

    logger.info("会话结束: %s", thread_id)
    return 0


def _run_ingest(full: bool = False) -> int:
    """索引知识库并退出。

    运维入口走 ``ingest_directory``（显式索引），
    而不是检索链路用的 ``ensure_knowledge_base``——后者在库非空时会直接跳过，
    那是「启动时兜底」的语义，不适用于运维侧的增量更新。
    索引逻辑全部在 ``app.rag.pipeline``，此处只负责参数、异常与报告输出。
    """

    try:
        rag_settings = get_rag_settings()
        report = ingest_directory(settings=rag_settings, full=full)
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 索引失败需明确退出码
        logger.exception("知识库索引失败")
        print(f"[失败] 知识库索引失败: {exc}", file=sys.stderr)
        return 1

    print(report.format_report())
    return 0


def _configured_threshold() -> float | None:
    """返回当前生效的相关性阈值；RAG 未配置时视为未启用（不过滤）。

    仅用于**展示**当前检索策略：阈值的比较与过滤全部发生在检索层
    （``KnowledgeRetriever._accepts``），本文件不参与任何过滤决策，
    因此这里只是「把配置读出来打印」。
    """

    try:
        return get_rag_settings().score_threshold
    except ConfigurationError:
        return None


def _format_threshold(value: float | None) -> str:
    """把阈值渲染成可读文本；``None`` 表示未启用。"""

    return "未启用" if value is None else f"{value:.3f}"


def _run_evaluate_rag() -> int:
    """对内置评估数据集执行检索评估并退出。

    评估链路固定为 ``question -> Retriever -> 指标``，不经过 Agent 与 LLM，
    因此结果可重复、前后版本可对比。数据集与指标口径见 ``app.rag.evaluation``。
    """

    try:
        cases = load_dataset()
    except DatasetError as exc:
        print(f"[数据集错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 数据集损坏需明确退出码
        logger.exception("评估数据集加载失败")
        print(f"[失败] 评估数据集加载失败: {exc}", file=sys.stderr)
        return 1

    logger.info("评估数据集: %s", summarize_dataset(cases))

    try:
        report = evaluate_dataset(cases)
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 评估失败需明确退出码
        logger.exception("检索评估失败")
        print(f"[失败] 检索评估失败: {exc}", file=sys.stderr)
        return 1

    print(report.format_report())

    # 报告末尾声明本次评估所使用的检索策略：不设置 RAG_SCORE_THRESHOLD 与设置后
    # 各跑一次即可直接对比，无需回看环境变量。展示不参与任何过滤判定。
    print()
    print(f"检索策略: score_threshold={_format_threshold(_configured_threshold())}")

    # 全部用例零召回几乎总是「知识库尚未索引」而不是检索质量问题，给出可执行的提示
    if report.scored_cases and report.retrieved_cases == 0:
        print(
            "\n提示: 所有用例均未召回到任何片段，知识库可能尚未索引，"
            "请先执行 python main.py --ingest",
            file=sys.stderr,
        )
    return 0


def _run_sync_rag_dataset() -> int:
    """把内置评估数据集同步到 LangSmith Dataset 后退出。

    与 ``--evaluate-rag`` 的分工：**这里只搬数据集**，不跑检索、不算指标、不创建
    Experiment（那些属于后续阶段）。搬运逻辑全部在
    ``app.rag.evaluation.langsmith_dataset``，此处只负责调用、异常与输出。

    数据集是人工维护的标准答案，因此「同步」绝不能变成「每次运行时自动上传」：
    只有显式传入 ``--sync-rag-dataset`` 才会走到这里，普通对话链路一次都不碰。
    未配置 LangSmith 时给出明确提示并返回配置错误码，而不是让进程带着半截状态继续。
    """

    try:
        cases = load_dataset()
    except DatasetError as exc:
        print(f"[数据集错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 数据集损坏需明确退出码
        logger.exception("评估数据集加载失败")
        print(f"[失败] 评估数据集加载失败: {exc}", file=sys.stderr)
        return 1

    logger.info("待同步数据集: %s", summarize_dataset(cases))

    try:
        result = sync_rag_dataset(cases, client=build_dataset_client())
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except DatasetSyncError as exc:
        print(f"[数据集错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 同步失败需明确退出码
        logger.exception("评估数据集同步失败")
        print(f"[失败] 评估数据集同步失败: {exc}", file=sys.stderr)
        return 1

    print(result.format_report())
    return 0


def _run_evaluate_rag_langsmith(*, full: bool = False) -> int:
    """在 LangSmith 上执行一次检索评估后退出。

    与 ``--evaluate-rag`` 的关系：**指标口径完全相同**（都来自
    ``app.rag.evaluation.evaluator.score_case``），区别只在「谁来跑、结果落在哪」——
    ``--evaluate-rag`` 在本地打印报告，本入口把同样的判定交给 LangSmith 的
    Evaluation：官方 SDK 负责跑 Run、执行 evaluator、并把五个指标分别记进实验。

    默认只跑**最小样本**（1 条正样本 + 1 条负样本），用于确认「链路通、指标有值、
    负样本被单独判定」；要跑全量数据集需显式加 ``--langsmith-full``。
    这也是一道防呆：数据集是人工维护的标准答案，全量执行会消耗额度，
    不该是敲错一个参数就发生的事。

    本入口**不参与**普通对话链路：``python main.py "你好"`` 一次都不会走到这里。
    """

    selection = FULL_SELECTION if full else SMOKE_SELECTION

    try:
        # 客户端与检索器都在这里显式装配：配置缺失的报错发生在「开始跑之前」，
        # 而不是变成一批读不出结果的失败 Run。
        client = build_dataset_client()
        retriever = default_retriever()
        summary = run_retrieval_evaluation(
            client=client,
            dataset_name=DEFAULT_DATASET_NAME,
            target=build_retrieval_target(retriever=retriever),
            selection=selection,
        )
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 评估失败需明确退出码
        logger.exception("LangSmith 检索评估失败")
        print(f"[失败] LangSmith 检索评估失败: {exc}", file=sys.stderr)
        return 1

    print(summary.format_report())
    return 0


def _judge_llm() -> BaseChatModel:
    """构造回答相关性 Judge 使用的**独立**模型实例。

    复用项目现有 LLM 配置（provider / model / api_key / base_url / timeout /
    max_retries 全部来自 ``build_llm()``），只把温度固定为 ``JUDGE_TEMPERATURE``
    （0）以保证判定可复现——这是本阶段为 Judge 做的**唯一**模型参数调整，
    没有第二套模型配置。

    与业务模型是**两个实例**：Judge 不共用业务 Agent 的那次调用，
    因此判定本身不会污染、也不会复用 Agent 的回答生成。
    """

    return build_llm().model_copy(update={"temperature": JUDGE_TEMPERATURE})


def _run_evaluate_rag_answer() -> int:
    """在 LangSmith 上执行一次**回答相关性**评估后退出。

    与 ``--evaluate-rag-langsmith`` 的关系：那是**检索质量**评估（只看 ``sources``），
    本入口是**回答质量**评估（只看最终回答），两者是两次独立实验、各自只提交
    自己的指标，互不影响，也不合成总分。

    默认只跑数据集里两条主题不同的真实用例（``library-hours`` / ``gym-hours``），
    用于确认「链路通、Judge 给出分数与理由」。本入口刻意**不提供全量开关**：
    回答相关性会真实调用模型，全量执行应当是一次显式决定。

    回答由 ``RAGService.answer`` 生成——即 knowledge 节点生成回答所用的同一段代码，
    所以被判定的就是 Agent 的真实回答；``auto_ingest=False`` 是因为评估是只读动作，
    不该顺手改知识库。本入口**不参与**普通对话链路。
    """

    try:
        # 客户端 / 服务 / Judge 都在这里显式装配：配置缺失的报错发生在「开始跑之前」，
        # 而不是变成一批读不出结果的失败 Run。
        client = build_dataset_client()
        service = build_rag_service(auto_ingest=False)
        summary = run_answer_relevance_evaluation(
            client=client,
            dataset_name=DEFAULT_DATASET_NAME,
            target=build_answer_target(answer_fn=service.answer),
            evaluator=build_answer_relevance_evaluator(AnswerRelevanceJudge(_judge_llm())),
        )
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 评估失败需明确退出码
        logger.exception("LangSmith 回答相关性评估失败")
        print(f"[失败] LangSmith 回答相关性评估失败: {exc}", file=sys.stderr)
        return 1

    print(summary.format_report())
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
    else:
        logger.info(
            "RAG | vectorstore=%s collection=%s embedding=%s chunk=%d/%d top_k=%d "
            "score_threshold=%s knowledge_dir=%s manifest=%s",
            rag_settings.vectorstore_backend,
            rag_settings.collection_name,
            rag_settings.embedding.model,
            rag_settings.chunk_size,
            rag_settings.chunk_overlap,
            rag_settings.top_k,
            _format_threshold(rag_settings.score_threshold),
            rag_settings.knowledge_dir,
            resolve_manifest_path(rag_settings),
        )

    # 校园业务服务（Spring Boot）同样是可选依赖：未配置时仅告警，
    # 对话与知识问答照常可用，只有依赖真实业务数据的工具会返回可读错误。
    try:
        campus_settings = get_campus_api_settings()
    except ConfigurationError as exc:
        logger.warning("校园业务服务未配置，依赖业务数据的工具将不可用: %s", exc)
    else:
        logger.info(
            "校园业务服务 | base_url=%s timeout=%ss student_id=%s",
            campus_settings.base_url,
            campus_settings.timeout,
            campus_settings.default_student_id,
        )

    # MCP 是可选的调用方式：关闭时考试查询直连业务服务（少一跳），
    # 开启时改走 Campus MCP Server。这里只打印**意图**（是否开启与启动命令），
    # 实际建连与工具发现发生在装配运行时的 build_agent_tools() 里，
    # 因此本函数依旧是「只读配置、不建连接」。
    try:
        mcp_settings = get_mcp_settings()
    except ConfigurationError as exc:
        logger.warning("MCP 未配置，考试查询将直连校园业务服务: %s", exc)
    else:
        logger.info(
            "MCP | enabled=%s server=%s command=%s %s timeout=%ss/%ss",
            mcp_settings.enabled,
            mcp_settings.server_name,
            mcp_settings.command,
            " ".join(mcp_settings.args),
            mcp_settings.startup_timeout,
            mcp_settings.call_timeout,
        )


def _apply_tracing() -> None:
    """把追踪配置落地到进程环境。

    **唯一写入点在本文件**：``app/`` 下的代码一个字都不许出现追踪相关的名字——
    架构守卫（``tests/test_langsmith_tracing.py::test_application_code_has_no_langsmith_usage``）
    把 ``app/**`` 里出现的名字一律判成「第二套追踪机制」。因此 HTTP 入口
    （``app/api/server.py``）也只能经由本包装来落地，而不是自己去 import 配置层函数。
    """

    apply_langsmith_tracing()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        settings: AppSettings = get_settings()
    except ConfigurationError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    _configure_logging(args.log_level or settings.log_level)

    # LangSmith 追踪在这里一次性落地：把配置归一化后写进进程环境，其余交给
    # LangChain / LangGraph 官方机制。放在启动最前面（早于装配运行时的任何
    # 模型 / 工具构建），因此单次、交互、索引三类入口都能自动产生 trace，
    # 而图、节点、工具、RAG、MCP 里没有任何 tracing 代码。
    # 未配置或配置非法时它只会告警并降级为「不上报」，不影响启动。
    _apply_tracing()

    _log_startup(settings)

    if args.full and not args.ingest:
        print("[参数错误] --full 需与 --ingest 一起使用", file=sys.stderr)
        return 2

    # 回答相关性入口刻意没有全量模式（它会真实调用模型），
    # 同时传 --langsmith-full 说明意图与入口不匹配，直接报错而不是静默按最小样本跑。
    # 本判定放在「--langsmith-full 需与 --evaluate-rag-langsmith 一起用」之前：
    # 两个参数同时出现时，「这个入口没有全量模式」比「它缺了另一个伙伴」更贴近事实。
    if args.evaluate_rag_answer and args.langsmith_full:
        print(
            "[参数错误] --evaluate-rag-answer 没有全量模式，请勿与 --langsmith-full 一起使用",
            file=sys.stderr,
        )
        return 2

    if args.langsmith_full and not args.evaluate_rag_langsmith:
        print(
            "[参数错误] --langsmith-full 需与 --evaluate-rag-langsmith 一起使用",
            file=sys.stderr,
        )
        return 2

    if args.ingest:
        return _run_ingest(full=args.full)

    if args.evaluate_rag:
        return _run_evaluate_rag()

    if args.sync_rag_dataset:
        return _run_sync_rag_dataset()

    if args.evaluate_rag_langsmith:
        return _run_evaluate_rag_langsmith(full=args.langsmith_full)

    if args.evaluate_rag_answer:
        return _run_evaluate_rag_answer()

    try:
        runtime = _build_runtime(settings)
    except CheckpointerError as exc:
        # 装配顺序是「先建工具清单、后建检查点后端」：后端这一步失败时，
        # 工具清单可能已经拉起了 MCP 子进程，而 runtime 从未产生、也就进不了
        # 下面那段 finally。这里补一次同一条收尾路径——「启动失败」不该反而
        # 留下一个没人管的会话（未装配过客户端时它是空操作）。
        reset_mcp_client()
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    session_id = args.session_id or uuid.uuid4().hex

    # 运行时可能持有进程级资源（检查点后端的 SQLite 连接、被 MCP 工具清单拉起的
    # MCP Server 子进程），因此无论单轮还是交互模式，退出前都显式释放：
    # REPL 是长驻入口，留一条永不关闭的连接、或一个只靠 daemon 线程兜底的子进程，
    # 都不是「进程结束自然回收」能解释的；单轮模式走同一段收尾，两条路径行为一致。
    try:
        if args.message:
            return _run_once(runtime, args.message, session_id)
        return _run_repl(runtime, session_id)
    finally:
        _close_runtime(runtime)


if __name__ == "__main__":
    raise SystemExit(main())
