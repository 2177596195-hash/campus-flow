"""把本地检索指标接入 LangSmith Evaluation（薄适配层，不做第二套口径）。

本模块只做一件事——**把 LangSmith 的一次 Run 翻译成本地评估器认识的东西**：:

    LangSmith Run ─┐
                   ├─► 提取检索结果 ─► 复用 evaluator.score_case ─► {key, score, comment}
    Example ───────┘

指标口径**一个都没有重写**：hit@1 / hit@k / precision / recall / 负样本判定
全部来自 ``app.rag.evaluation.evaluator.score_case``（本地离线评估的事实基准），
本模块只负责「取值 → 传参 → 组织 comment」。因此本地 ``--evaluate-rag`` 与
LangSmith 实验给出的数字是同一套语义，可以直接对看。

Run 输出为什么够用（已核对的真实结构，不是猜的）
------------------------------------------------
``RAGService.retrieve`` 就是 ``KnowledgeRetriever.retrieve`` 的直通，
而 knowledge 节点把 ``RAGAnswer.sources`` 原样写进状态，每一项来自
``RetrievedChunk.as_source()``::

    {"source": "campus_rules.md", "title": "...", "score": 0.72, "snippet": "……"}

因此 Run 输出里的 ``sources`` **同时带着来源与相关性分数**，本模块无需改动
Retriever / RAGService / knowledge 节点即可拿到全部指标所需的数据。

**唯一的数据缺口（如实记录，不靠改上游掩盖）**：``snippet`` 被截断到
``SNIPPET_MAX_CHARS``（200 字符），而分块长度是 500——也就是说
``expected_keywords`` 的命中统计会因截断而偏低。该字段在本地口径里
**只是诊断信息、不参与任何指标**（见 ``evaluator._match_keywords``），
所以指标本身不受影响；本模块的 comment 也**不使用**关键词统计，避免用截断文本
给出看似精确的结论。

各指标是**独立 evaluator**（LangSmith 里分别观察、分别聚合），
不合成「总分」：``rag_hit_at_1`` / ``rag_hit_at_k`` / ``rag_precision`` /
``rag_recall`` / ``rag_negative_sample``。

负数样本严格沿用本地口径：``expected_sources`` 为空即负样本，
hit / precision / recall 在本地是**未定义且不参与任何汇总均值**，
所以这里返回 ``score=None``（不是 0，更不是「recall=1」）；
负样本单独由 ``rag_negative_sample`` 判定（未召回任何片段 = 1.0）。

回答相关性（``rag_answer_relevance``，本阶段新增的**唯一**一个 LLM-as-a-Judge）
--------------------------------------------------------------------------------
前五个指标评的是**检索质量**，它们的输入只有 ``sources``；而「回答是否真正回答了
用户的问题」必须拿到**最终回答**，那是另一份数据。

**最终回答实际在哪里（实测核对，不是猜）**：真跑一轮知识问答后，从平台上读回的
生产 root run（``campus-flow.turn``）``outputs`` 键恰为::

    ["error", "intent", "messages", "session_id", "sources"]

即回答在 ``messages`` 里**最后一条 ``type == "ai"`` 的消息的 ``content``**，
而 retrieval 评估 target 的输出（``["retrieved", "sources"]``）**根本不含回答**
——那个 Run 刻意不调模型。因此这里提供了 ``extract_run_answer``：优先读评估 target
显式给出的 ``answer`` 字段，其次回退到生产 root run 的 ``messages`` 形状，
两种 Run 都能判。

**数据缺口（如实记录，不靠改上游掩盖）**：生产 root run 里**没有**独立的
``answer`` 字段，也没有 ``retrieved``；回答只能从 ``messages`` 末条 AI 消息取。
本阶段**不修改** 节点 / 图 / RAGService 去补这个字段——回退路径已经够用，
少一处改动就少一处回归面。

判定与评分由 ``app.rag.evaluation.answer_judge`` 负责（**评估模型由调用方注入**，
本模块因此依然不 import 任何模型工厂）；本模块只做「取问题 → 取回答 → 交给 Judge
→ 组织 ``{key, score, comment}``」。Judge 无法判定时一律 ``score=None`` +
「无法判定：<原因>」，**绝不记 0 分**（0 分表示确实答非所问，含义完全不同）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.rag.evaluation.answer_judge import AnswerJudge
from app.rag.evaluation.evaluator import default_retriever, score_case
from app.rag.evaluation.schema import DEFAULT_HIT_K, CaseResult, EvaluationCase
from app.rag.retriever import KnowledgeRetriever
from app.rag.schema import RetrievedChunk

logger = logging.getLogger(__name__)

#: Run 输出里的引用列表键（与 ``AgentState.sources`` / ``RAGAnswer.sources`` 同名）
SOURCES_KEY: Final[str] = "sources"
#: Run 输出里的片段条数键（可选：真实 Agent 的 knowledge 节点不写它，本模块容忍缺失）
RETRIEVED_KEY: Final[str] = "retrieved"
#: 评估 target 显式给出的最终回答键（检索评估 target 不含它）
ANSWER_KEY: Final[str] = "answer"
#: 生产 root run 的最终状态里承载回答的字段（回答 = 末条 AI 消息的 content）
MESSAGES_KEY: Final[str] = "messages"
#: 消息序列化后的角色标识（LangChain ``AIMessage.type``）
AI_MESSAGE_TYPE: Final[str] = "ai"

#: 五个独立指标的 key（LangSmith 里按 key 分别观察）
HIT_AT_1_KEY: Final[str] = "rag_hit_at_1"
HIT_AT_K_KEY: Final[str] = "rag_hit_at_k"
PRECISION_KEY: Final[str] = "rag_precision"
RECALL_KEY: Final[str] = "rag_recall"
NEGATIVE_SAMPLE_KEY: Final[str] = "rag_negative_sample"

#: 回答相关性（唯一一个 LLM-as-a-Judge，与上面五个检索指标互不影响）
ANSWER_RELEVANCE_KEY: Final[str] = "rag_answer_relevance"

#: 回答相关性实验的实验名前缀：与检索实验分开，便于在平台上辨认与对比
ANSWER_EXPERIMENT_PREFIX: Final[str] = "campus-flow-rag-answer"
#: 回答相关性实验的报告标题（与检索实验共用同一套渲染，只换标题）
ANSWER_EXPERIMENT_TITLE: Final[str] = "LangSmith RAG Answer Relevance Evaluation"
#: 回答相关性实验只提交这一个指标（不合成总分，与检索实验各自独立）
ANSWER_EVALUATOR_KEYS: Final[tuple[str, ...]] = (ANSWER_RELEVANCE_KEY,)

#: 判定窗口 == 召回深度，固定与本地 ``--evaluate-rag`` 的 k 对齐
#: （``DEFAULT_HIT_K``；k 既是 hit@k 的窗口也是 precision 的分母范围）。
EVALUATION_K: Final[int] = DEFAULT_HIT_K

#: 「无法判定」时统一使用的说明前缀，便于在平台上一眼看出不是检索失败
_UNAVAILABLE_PREFIX: Final[str] = "无法判定"


# ============================================================ 1. 提取（Run -> 检索结果）


@dataclass(frozen=True, slots=True)
class RunRetrieval:
    """从一次 RAG Run 中提取出的检索结果。

    ``unreadable`` 非空表示**这次 Run 没法用来判定**（输出为空、缺少 ``sources``、
    或结构非法）。此时代码不会把它当成「零召回」——零召回是一个有意义的结论，
    而「读不出来」不是，两者混在一起会把基础设施故障伪装成检索质量问题。
    """

    chunks: tuple[RetrievedChunk, ...] = ()
    retrieved: int = 0
    unreadable: str | None = None

    @property
    def sources(self) -> tuple[str, ...]:
        """本次召回的来源（保持原始顺序）。"""

        return tuple(chunk.source for chunk in self.chunks)


def _coerce_score(value: Any) -> float | None:
    """把 Run 里的分数规整为 ``float | None``（布尔与非数字一律视为未知）。"""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _to_chunk(entry: Any) -> RetrievedChunk | None:
    """把 Run 输出里的一条引用转换为 ``RetrievedChunk``；结构非法返回 ``None``。"""

    if not isinstance(entry, Mapping):
        return None
    source = entry.get("source")
    if not isinstance(source, str):
        # 本地口径里 source 缺失会被记为「未知来源」，这里同样保留该语义，
        # 但必须是非字符串以外的情况才走到：None 也当作缺失处理。
        source = ""
    title = entry.get("title")
    snippet = entry.get("snippet")
    return RetrievedChunk(
        content=snippet if isinstance(snippet, str) else "",
        source=source,
        title=title if isinstance(title, str) else source,
        score=_coerce_score(entry.get("score")),
    )


def extract_run_retrieval(run: Any) -> RunRetrieval:
    """从 LangSmith Run（或任何带 ``outputs`` 的对象）提取检索结果。

    容忍三种真实情况：
    1. 真实 Agent 的 knowledge 节点只写 ``sources``，不写 ``retrieved`` → 用长度补齐；
    2. ``sources`` 为空数组 → **合法的零召回**，不是错误；
    3. Run 报错或输出缺失 → ``unreadable`` 说明原因，由上层决定「不判定」。
    """

    error = getattr(run, "error", None)
    if error:
        return RunRetrieval(unreadable=f"Run 执行失败: {error}")

    outputs = getattr(run, "outputs", None)
    if not isinstance(outputs, Mapping):
        return RunRetrieval(unreadable="Run 输出为空或不是对象")

    raw_sources = outputs.get(SOURCES_KEY)
    if raw_sources is None:
        return RunRetrieval(
            unreadable=f"Run 输出缺少 {SOURCES_KEY} 字段（拿到的是: {sorted(outputs)}）"
        )
    if not isinstance(raw_sources, (list, tuple)):
        return RunRetrieval(unreadable=f"Run 输出的 {SOURCES_KEY} 不是数组")

    chunks: list[RetrievedChunk] = []
    for entry in raw_sources:
        chunk = _to_chunk(entry)
        if chunk is None:
            return RunRetrieval(
                unreadable=f"Run 输出的 {SOURCES_KEY} 含非法条目: {entry!r}"
            )
        chunks.append(chunk)

    raw_retrieved = outputs.get(RETRIEVED_KEY)
    retrieved = (
        int(raw_retrieved)
        if isinstance(raw_retrieved, int) and not isinstance(raw_retrieved, bool)
        else len(chunks)
    )
    return RunRetrieval(chunks=tuple(chunks), retrieved=retrieved)


# ------------------------------------------------ 提取（Run -> 最终回答）

#: 回答缺失时的统一原因。与用户侧约定逐字一致，便于在平台上按字符串检索。
MISSING_ANSWER_REASON: Final[str] = "缺少最终回答"


@dataclass(frozen=True, slots=True)
class RunAnswer:
    """从一次 Run 中提取出的最终回答。

    ``unreadable`` 非空表示**这次 Run 判不了回答相关性**（Run 报错、输出缺失，
    或两种形状里都找不到非空回答）。此时代码返回 ``score=None``，
    而不是把「读不出来」当成「答非所问」记 0 分。
    """

    answer: str = ""
    unreadable: str | None = None


def _message_role(message: Any) -> str:
    """读出一条消息的角色标识（序列化后的 dict 与真实消息对象都支持）。"""

    value = message.get("type") if isinstance(message, Mapping) else getattr(message, "type", None)
    return value if isinstance(value, str) else ""


def _message_content(message: Any) -> str:
    """读出一条消息的文本内容（非字符串一律视为空）。"""

    value = (
        message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    )
    return value if isinstance(value, str) else ""


def extract_run_answer(run: Any) -> RunAnswer:
    """从 LangSmith Run 提取最终回答，容忍两种真实形状。

    1. 评估 target 的显式字段 ``outputs["answer"]``；
    2. **生产 root run** 的 ``outputs["messages"]``——取最后一条
       ``type == "ai"`` 且内容非空的消息（LangGraph 把最终状态整体作为 root run
       输出，回答就在消息列表里；工具调用的那条 AI 消息内容为空，会被跳过）。

    两种都拿不到非空回答时返回 ``unreadable=MISSING_ANSWER_REASON``，
    与「Run 报错 / 输出不是对象」这类真正的读不出来区分开（后者带诊断信息）。
    """

    error = getattr(run, "error", None)
    if error:
        return RunAnswer(unreadable=f"Run 执行失败: {error}")

    outputs = getattr(run, "outputs", None)
    if not isinstance(outputs, Mapping):
        return RunAnswer(unreadable="Run 输出为空或不是对象")

    raw_answer = outputs.get(ANSWER_KEY)
    if isinstance(raw_answer, str):
        # 显式给了 ``answer`` 字段：空串 / 纯空白等价于「没有回答」，是缺数据而不是结构错误
        stripped = raw_answer.strip()
        return RunAnswer(
            answer=stripped,
            unreadable=None if stripped else MISSING_ANSWER_REASON,
        )

    raw_messages = outputs.get(MESSAGES_KEY)
    if isinstance(raw_messages, (list, tuple)):
        for message in reversed(raw_messages):
            if _message_role(message) == AI_MESSAGE_TYPE:
                content = _message_content(message).strip()
                if content:
                    return RunAnswer(answer=content)
        return RunAnswer(unreadable=MISSING_ANSWER_REASON)

    if raw_answer is None and raw_messages is None:
        return RunAnswer(unreadable=MISSING_ANSWER_REASON)
    return RunAnswer(
        unreadable=(
            f"Run 输出的 {ANSWER_KEY} / {MESSAGES_KEY} 结构非法（拿到的是: {sorted(outputs)}）"
        )
    )


# ============================================================ 2. 转换（Example -> 用例）


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """把引用输出里的字段规整为字符串元组（非数组一律视为空）。"""

    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _question_of(example: Any) -> str:
    """取出 Example 里的用户问题；缺失或纯空白时返回空串。

    检索指标与回答相关性指标都需要它，因此抽成一处，
    避免「同一个问题在两处各解析一次」而口径漂移。
    """

    inputs = getattr(example, "inputs", None)
    question = inputs.get("question") if isinstance(inputs, Mapping) else None
    if not isinstance(question, str) or not question.strip():
        return ""
    return question.strip()


def build_case(example: Any) -> EvaluationCase | None:
    """从 LangSmith Example 还原本地用例（``inputs`` / ``outputs`` / ``metadata``）。

    metadata 是可选增强：缺了也能判 hit / precision / recall，
    只是没有 ``case_id`` 与「分数标定」诊断。``question`` 缺失才返回 ``None``
    ——那是真的没法判定。
    """

    question = _question_of(example)
    if not question:
        return None

    outputs = getattr(example, "outputs", None)
    expected_sources = (
        _as_str_tuple(outputs.get("expected_sources")) if isinstance(outputs, Mapping) else ()
    )

    metadata = getattr(example, "metadata", None)
    meta: Mapping[str, Any] = metadata if isinstance(metadata, Mapping) else {}
    case_id = meta.get("case_id")

    return EvaluationCase(
        question=question,
        expected_sources=expected_sources,
        expected_keywords=_as_str_tuple(meta.get("expected_keywords")),
        expected_min_score=_as_float(meta.get("expected_min_score")),
        case_id=case_id if isinstance(case_id, str) else "",
    )


# ============================================================ 3. 判定（复用现有指标）


@dataclass(frozen=True, slots=True)
class Judgement:
    """一次 ``(run, example)`` 的判定结果：要么给出 ``CaseResult``，要么给出原因。"""

    result: CaseResult | None = None
    #: 无法判定的原因（Run 不可读 / Example 缺 question）
    unavailable: str | None = None

    @property
    def is_negative(self) -> bool:
        """是否负样本（判定不可用时一律为 ``False``）。"""

        return self.result is not None and self.result.is_negative


def judge(run: Any, example: Any) -> Judgement:
    """把 Run + Example 判成本地 ``CaseResult``（唯一调用 ``score_case`` 的地方）。"""

    case = build_case(example)
    if case is None:
        return Judgement(unavailable="Example 缺少非空的 inputs.question")

    retrieval = extract_run_retrieval(run)
    if retrieval.unreadable:
        return Judgement(unavailable=retrieval.unreadable)

    return Judgement(result=score_case(case, retrieval.chunks, k=EVALUATION_K))


# ============================================================ 4. 输出（LangSmith 契约）


def _unavailable_score(key: str, reason: str) -> dict[str, Any]:
    """指标无法判定时的统一输出：``score=None`` + 明确原因。"""

    return {"key": key, "score": None, "comment": f"{_UNAVAILABLE_PREFIX}：{reason}"}


def _undefined_for_negative(key: str) -> dict[str, Any]:
    """负样本上「未定义」的四个指标：与本地口径一致——不给出分数。"""

    return {
        "key": key,
        "score": None,
        "comment": "负样本：expected_sources 为空，该指标未定义且不参与汇总（与本地口径一致）",
    }


def _format_sources(sources: Sequence[str], scores: Sequence[float | None]) -> str:
    """渲染 ``a.md (0.910), b.md (-)`` 形式（与本地报告同一风格）。"""

    if not sources:
        return "(无召回)"
    rendered = [
        f"{source}({score:.3f})" if score is not None else f"{source}(-)"
        for source, score in zip(sources, scores)
    ]
    return ", ".join(rendered)


def _diagnostics(result: CaseResult) -> str:
    """公共诊断信息：期望、实际（含分数）、排名、漏召、条数与分数标定。

    刻意**不含**关键词统计：Run 里的 ``snippet`` 被截断，用截断文本统计关键词
    会给出看似精确的错误结论（该字段在本地也只是诊断项、不参与指标）。
    """

    parts = [
        f"expected=[{', '.join(result.expected_sources) or '空（负样本）'}]",
        f"actual=[{_format_sources(result.actual_sources, result.scores)}]",
        f"first_relevant_rank={result.first_relevant_rank}",
        f"retrieved={result.retrieved}",
    ]
    if result.missed_sources:
        parts.append(f"missed=[{', '.join(result.missed_sources)}]")
    if result.min_score_ok is False:
        parts.append("分数标定: 命中来源的相关性未达到用例声明下限")
    return " | ".join(parts)


def _scored(key: str, score: float, result: CaseResult) -> dict[str, Any]:
    return {"key": key, "score": score, "comment": _diagnostics(result)}


# ---------------------------------------------------------------- 五个独立 evaluator
#
# 签名一律 ``(run, example)``：这是官方 SDK 的经典形式，
# 也正好是「从 Run 里取实际结果」这一设计所要求的形态。


def rag_hit_at_1(run: Any, example: Any) -> dict[str, Any]:
    """Top1 是否命中期望来源。"""

    judgement = judge(run, example)
    if judgement.unavailable:
        return _unavailable_score(HIT_AT_1_KEY, judgement.unavailable)
    result = judgement.result
    assert result is not None  # 由 unavailable 分支保证
    if judgement.is_negative:
        return _undefined_for_negative(HIT_AT_1_KEY)
    return _scored(HIT_AT_1_KEY, 1.0 if result.hit_at_1 else 0.0, result)


def rag_hit_at_k(run: Any, example: Any) -> dict[str, Any]:
    """Top K（按 source 去重后）是否至少命中一个期望来源。"""

    judgement = judge(run, example)
    if judgement.unavailable:
        return _unavailable_score(HIT_AT_K_KEY, judgement.unavailable)
    result = judgement.result
    assert result is not None
    if judgement.is_negative:
        return _undefined_for_negative(HIT_AT_K_KEY)
    return _scored(HIT_AT_K_KEY, 1.0 if result.hit_at_k else 0.0, result)


def rag_precision(run: Any, example: Any) -> dict[str, Any]:
    """去重后实际来源中命中期望的比例。"""

    judgement = judge(run, example)
    if judgement.unavailable:
        return _unavailable_score(PRECISION_KEY, judgement.unavailable)
    result = judgement.result
    assert result is not None
    if judgement.is_negative or result.precision is None:
        return _undefined_for_negative(PRECISION_KEY)
    return _scored(PRECISION_KEY, float(result.precision), result)


def rag_recall(run: Any, example: Any) -> dict[str, Any]:
    """期望来源被召回的比例。"""

    judgement = judge(run, example)
    if judgement.unavailable:
        return _unavailable_score(RECALL_KEY, judgement.unavailable)
    result = judgement.result
    assert result is not None
    if judgement.is_negative or result.recall is None:
        return _undefined_for_negative(RECALL_KEY)
    return _scored(RECALL_KEY, float(result.recall), result)


def rag_negative_sample(run: Any, example: Any) -> dict[str, Any]:
    """负样本是否「什么都没召回」。

    与本地 ``unexpected_hits`` 完全同一判定：只要窗口内召回到任意片段就算不合预期。
    未召回到任何片段 = 1.0（符合预期），否则 0.0 并把召回内容写进 comment。
    """

    judgement = judge(run, example)
    if judgement.unavailable:
        return _unavailable_score(NEGATIVE_SAMPLE_KEY, judgement.unavailable)
    result = judgement.result
    assert result is not None
    if not judgement.is_negative:
        return {
            "key": NEGATIVE_SAMPLE_KEY,
            "score": None,
            "comment": "非负样本：expected_sources 非空，该指标不适用",
        }

    unexpected = result.retrieved > 0
    prefix = "负样本误召回" if unexpected else "负样本未召回任何片段（符合预期）"
    return {
        "key": NEGATIVE_SAMPLE_KEY,
        "score": 0.0 if unexpected else 1.0,
        "comment": f"{prefix} | {_diagnostics(result)}",
    }


#: 默认提交给 LangSmith 的 evaluator 列表（**每个指标独立**，不合成总分）
DEFAULT_EVALUATORS: Final[tuple[Callable[[Any, Any], dict[str, Any]], ...]] = (
    rag_hit_at_1,
    rag_hit_at_k,
    rag_precision,
    rag_recall,
    rag_negative_sample,
)


# -------------------------------------- 回答相关性（唯一一个 LLM-as-a-Judge）
#
# 与前五个指标的关键差别：它需要**最终回答**（不是检索结果），因此要注入一个
# 评测模型。模型由调用方构造并注入（`main.py` 走既有 LLM 配置），本模块因此
# 依旧不 import 任何模型工厂，评估层的依赖边界保持不变。
#
# 它**不进入** ``DEFAULT_EVALUATORS``：检索评估（--evaluate-rag-langsmith）与
# 回答相关性评估（--evaluate-rag-answer）是两次独立实验，
# 各自只提交自己的 evaluator，互不影响、也不合成总分。


def build_answer_relevance_evaluator(
    judge: AnswerJudge,
) -> Callable[[Any, Any], dict[str, Any]]:
    """构造 ``rag_answer_relevance`` evaluator（签名仍是官方的 ``(run, example)``）。

    输入严格只有两项：Example 的用户问题 + Run 的最终回答。
    **不传检索片段**——那是 Faithfulness / Citation 一类指标的输入。
    """

    def rag_answer_relevance(run: Any, example: Any) -> dict[str, Any]:
        """判断最终回答是否针对用户的问题。"""

        question = _question_of(example)
        if not question:
            return _unavailable_score(ANSWER_RELEVANCE_KEY, "Example 缺少非空的 inputs.question")

        extraction = extract_run_answer(run)
        if extraction.unreadable:
            return _unavailable_score(ANSWER_RELEVANCE_KEY, extraction.unreadable)

        try:
            judgement = judge.judge(question, extraction.answer)
        except Exception as exc:  # noqa: BLE001 - 判定器意外抛错也不该让整轮评估失败
            logger.warning("回答相关性判定异常", exc_info=True)
            return _unavailable_score(
                ANSWER_RELEVANCE_KEY, f"评测模型调用失败（{type(exc).__name__}）"
            )

        if judgement.score is None:
            return _unavailable_score(ANSWER_RELEVANCE_KEY, judgement.reason)
        return {
            "key": ANSWER_RELEVANCE_KEY,
            "score": judgement.score,
            "comment": judgement.reason,
        }

    return rag_answer_relevance


# ============================================================ 5. 目标函数（真实 RAG 检索）


def build_retrieval_target(
    *,
    retriever: KnowledgeRetriever | None = None,
    top_k: int = EVALUATION_K,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """构造 LangSmith 的 target：``{"question": ...} -> {"sources": [...], "retrieved": n}``。

    目标函数做的是**真实检索**（真向量库、真 Embedding），链路与 knowledge 节点里
    ``RAGService.retrieve`` 的那一步逐字相同——只是不调用模型生成回答：
    本阶段评的是检索质量，引入 LLM 只会让结果随温度与版本漂移。

    输出结构与 knowledge 节点写进状态的 ``sources`` **完全一致**
    （都用 ``RetrievedChunk.as_source()``），所以适配层读到的就是生产同款形状。

    检索器**惰性构建并复用**：整个实验共用一个实例，避免每条用例都重建向量库连接；
    同时刻意不触发 ``ensure_knowledge_base``——评估是只读动作，不该顺手改知识库。
    """

    cached: list[KnowledgeRetriever] = []

    def active_retriever() -> KnowledgeRetriever:
        if retriever is not None:
            return retriever
        if not cached:
            cached.append(default_retriever())
        return cached[0]

    def target(inputs: Mapping[str, Any]) -> dict[str, Any]:
        question = ""
        raw = inputs.get("question") if isinstance(inputs, Mapping) else None
        if isinstance(raw, str):
            question = raw.strip()
        if not question:
            raise ValueError("Example 缺少 inputs.question，无法检索")

        chunks = active_retriever().retrieve(question, top_k=top_k)
        return {
            SOURCES_KEY: [chunk.as_source() for chunk in chunks],
            RETRIEVED_KEY: len(chunks),
        }

    return target


def build_answer_target(
    *,
    answer_fn: Callable[[str], Any],
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """构造 LangSmith 的 answer target：
    ``{"question": ...} -> {"answer": ..., "sources": [...], "retrieved": n}``。

    与 ``build_retrieval_target`` 的唯一差别：**这里真的生成回答**。
    回答相关性评的是最终回答，没有回答就无从判定，所以 target 必须产出它。

    生成函数由调用方注入——生产里就是 ``RAGService.answer``，也就是 knowledge 节点
    生成回答所用的**同一段代码**，因此被判定的就是 Agent 的真实回答（不是另写一条
    生成链路）。本模块因此依旧不 import ``RAGService`` / 模型工厂：注入的是
    「怎么生成回答」，不是「用哪个模型生成」——后者由组合根（``main.py``）决定。

    输出额外带上 ``sources`` / ``retrieved``，与检索 target 同形，
    便于一眼看出这次回答依据了哪些片段；但**判定回答相关性时不使用它们**
    （那是 Faithfulness / Citation 一类指标的输入）。
    """

    def target(inputs: Mapping[str, Any]) -> dict[str, Any]:
        question = ""
        raw = inputs.get("question") if isinstance(inputs, Mapping) else None
        if isinstance(raw, str):
            question = raw.strip()
        if not question:
            raise ValueError("Example 缺少 inputs.question，无法生成回答")

        result = answer_fn(question)
        answer = getattr(result, "answer", None)
        sources = getattr(result, "sources", ()) or ()
        kept = [dict(item) for item in sources if isinstance(item, Mapping)]
        raw_retrieved = getattr(result, "retrieved", None)
        retrieved = (
            int(raw_retrieved)
            if isinstance(raw_retrieved, int) and not isinstance(raw_retrieved, bool)
            else len(kept)
        )
        return {
            ANSWER_KEY: answer if isinstance(answer, str) else "",
            SOURCES_KEY: kept,
            RETRIEVED_KEY: retrieved,
        }

    return target


# ============================================================ 6. 最小执行入口（一次性）


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
    """一次最小 Evaluation 的结果摘要（供 CLI 报告与测试断言）。"""

    dataset_name: str
    experiment_name: str
    cases: int
    positives: int
    negatives: int
    #: 指标 key -> 收集到的分数（未定义 / 无法判定的条目不进列表）
    scores: Mapping[str, tuple[float, ...]]
    #: 报告标题。检索与回答相关性是两次独立实验，共用同一套渲染、只换标题。
    title: str = "LangSmith RAG Evaluation"
    #: 报告里逐行列出的指标 key；``None`` 表示沿用检索的五个
    keys: tuple[str, ...] | None = None

    def format_report(self) -> str:
        """命令行展示用报告。"""

        lines = [
            self.title,
            "-" * len(self.title),
            f"Dataset: {self.dataset_name}",
            f"Experiment: {self.experiment_name}",
            f"用例: {self.cases}（正样本 {self.positives} / 负样本 {self.negatives}）",
        ]
        for key in self.keys or DEFAULT_EVALUATOR_KEYS:
            values = self.scores.get(key, ())
            if values:
                mean = sum(values) / len(values)
                lines.append(f"{key}: {mean:.3f}  (n={len(values)})")
            else:
                lines.append(f"{key}: (无有效分数)")
        return "\n".join(lines)


def _row_evaluations(row: Any) -> Sequence[Any]:
    """从结果行里取出 evaluator 的返回列表（容忍 dict / 对象两种形态）。"""

    payload = row.get("evaluation_results") if isinstance(row, Mapping) else getattr(
        row, "evaluation_results", None
    )
    if isinstance(payload, Mapping):
        results = payload.get("results")
    else:
        results = getattr(payload, "results", None)
    return results if isinstance(results, (list, tuple)) else ()


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def summarize_experiment(
    results: Any,
    *,
    dataset_name: str,
    experiment_name: str,
    title: str = "LangSmith RAG Evaluation",
    keys: tuple[str, ...] | None = None,
) -> ExperimentSummary:
    """把 ``client.evaluate`` 的返回值汇总成 CLI 报告用的摘要。"""

    per_key: dict[str, list[float]] = {}
    positives = negatives = cases = 0
    for row in results:
        cases += 1
        example = row.get("example") if isinstance(row, Mapping) else None
        outputs = getattr(example, "outputs", None)
        expected = outputs.get("expected_sources") if isinstance(outputs, Mapping) else None
        if isinstance(expected, (list, tuple)) and expected:
            positives += 1
        else:
            negatives += 1

        for item in _row_evaluations(row):
            key = _field(item, "key")
            score = _field(item, "score")
            if isinstance(key, str) and isinstance(score, (int, float)) and not isinstance(score, bool):
                per_key.setdefault(key, []).append(float(score))

    return ExperimentSummary(
        dataset_name=dataset_name,
        experiment_name=experiment_name,
        cases=cases,
        positives=positives,
        negatives=negatives,
        scores={key: tuple(values) for key, values in per_key.items()},
        title=title,
        keys=keys,
    )


#: 最小执行的选样口径：一条正样本 + 一条负样本（**不跑全量**）
SMOKE_SELECTION: Final[str] = "smoke"
FULL_SELECTION: Final[str] = "full"
#: 回答相关性最小验证的选样口径：**复用数据集里已有的两条真实用例**
ANSWER_SELECTION: Final[str] = "answer-smoke"

#: 回答相关性最小验证用的 case_id（主题不同，覆盖两类知识问题）。
#: 用 case_id 精确挑选而不是「取前两条」，是为了让选样不随数据集增删而漂移。
ANSWER_VERIFICATION_CASE_IDS: Final[tuple[str, ...]] = ("library-hours", "gym-hours")


def _case_id_of(example: Any) -> str:
    metadata = getattr(example, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ""
    value = metadata.get("case_id")
    return value if isinstance(value, str) else ""


def select_examples(examples: Sequence[Any], *, selection: str = SMOKE_SELECTION) -> list[Any]:
    """按口径挑出要跑的 Example。

    官方 SDK 的 ``data`` 参数既接受数据集名、也接受 **Example 列表**，
    所以「只跑 X 条」不需要自建接口：这里只是把挑好的几条传进去。
    """

    if selection == FULL_SELECTION:
        return list(examples)

    if selection == ANSWER_SELECTION:
        by_id = {_case_id_of(item): item for item in examples}
        picked = [by_id[case_id] for case_id in ANSWER_VERIFICATION_CASE_IDS if case_id in by_id]
        if not picked:
            raise ValueError(
                "数据集里找不到回答相关性最小验证所需的用例: "
                f"{', '.join(ANSWER_VERIFICATION_CASE_IDS)}"
            )
        return picked

    positives = [item for item in examples if _expected_sources_of(item)]
    negatives = [item for item in examples if not _expected_sources_of(item)]
    picked = [*positives[:1], *negatives[:1]]
    if not picked:
        raise ValueError("数据集里没有可用 Example，无法执行最小 Evaluation")
    return picked


def _expected_sources_of(example: Any) -> tuple[str, ...]:
    outputs = getattr(example, "outputs", None)
    if not isinstance(outputs, Mapping):
        return ()
    return _as_str_tuple(outputs.get("expected_sources"))


def _run_experiment(
    *,
    client: Any,
    dataset_name: str,
    target: Callable[[Mapping[str, Any]], dict[str, Any]],
    evaluators: Sequence[Callable[[Any, Any], dict[str, Any]]],
    selection: str,
    experiment_prefix: str,
    metadata: Mapping[str, Any],
    title: str,
    keys: tuple[str, ...] | None,
) -> ExperimentSummary:
    """提交一次 Evaluation 并汇总结果（检索与回答相关性共用的执行核）。

    两次实验只有「提交哪些 evaluator、写什么 metadata、报告怎么起标题」不同，
    数据集读取、选样、提交、汇总这四步逐字相同，因此收在这里一处实现——
    否则「两套口径」会从这份重复的编排代码里长出来。
    """

    dataset = client.read_dataset(dataset_name=dataset_name)
    examples = list(client.list_examples(dataset_id=dataset.id))
    selected = select_examples(examples, selection=selection)

    results = client.evaluate(
        target,
        data=selected,
        evaluators=list(evaluators),
        experiment_prefix=experiment_prefix,
        metadata=dict(metadata),
    )

    # blocking=True（默认）时结果已在本地，experiment_name 由 SDK 生成
    experiment_name = getattr(results, "experiment_name", "") or experiment_prefix
    return summarize_experiment(
        results,
        dataset_name=dataset_name,
        experiment_name=experiment_name,
        title=title,
        keys=keys,
    )


def run_retrieval_evaluation(
    *,
    client: Any,
    dataset_name: str,
    target: Callable[[Mapping[str, Any]], dict[str, Any]],
    selection: str = SMOKE_SELECTION,
    experiment_prefix: str = "campus-flow-rag-retrieval",
) -> ExperimentSummary:
    """在既有数据集上跑一次**检索质量**评估（Dataset / Run / Evaluator / 结果展示 全交给官方 SDK）。

    Args:
        client: 官方 ``langsmith.Client``（或同签名替身）。
        dataset_name: 上一阶段同步出来的数据集名。
        target: 目标函数（见 ``build_retrieval_target``）。
        selection: ``smoke``（1 正 1 负）或 ``full``（数据集全部）。
        experiment_prefix: 实验名前缀，便于在平台上辨认与对比。

    Returns:
        结果摘要（实验名 + 各指标的分数集合）。
    """

    return _run_experiment(
        client=client,
        dataset_name=dataset_name,
        target=target,
        evaluators=DEFAULT_EVALUATORS,
        selection=selection,
        experiment_prefix=experiment_prefix,
        metadata={
            "dataset": dataset_name,
            "selection": selection,
            "k": EVALUATION_K,
            "evaluator_keys": list(DEFAULT_EVALUATOR_KEYS),
        },
        title="LangSmith RAG Evaluation",
        keys=None,
    )


def run_answer_relevance_evaluation(
    *,
    client: Any,
    dataset_name: str,
    target: Callable[[Mapping[str, Any]], dict[str, Any]],
    evaluator: Callable[[Any, Any], dict[str, Any]],
    selection: str = ANSWER_SELECTION,
    experiment_prefix: str = ANSWER_EXPERIMENT_PREFIX,
) -> ExperimentSummary:
    """在既有数据集上跑一次**回答相关性**评估（与检索实验彼此独立）。

    与 ``run_retrieval_evaluation`` 只共用执行核，不会共享实验：
    这里**只提交 ``rag_answer_relevance`` 一个 evaluator**（``DEFAULT_EVALUATORS``
    一个都不带），因此既不会把检索指标混进回答实验，也不会合成一个「总分」。

    Args:
        client: 官方 ``langsmith.Client``（或同签名替身）。
        dataset_name: 已同步的数据集名（与检索实验同一个数据集）。
        target: 目标函数（见 ``build_answer_target``）——必须产出最终回答。
        evaluator: 由 ``build_answer_relevance_evaluator`` 构造的评估器。
        selection: 默认 ``answer-smoke``（复用数据集里两条主题不同的真实用例）。
        experiment_prefix: 实验名前缀，与检索实验区分开。

    Returns:
        结果摘要（实验名 + ``rag_answer_relevance`` 的分数集合）。
    """

    return _run_experiment(
        client=client,
        dataset_name=dataset_name,
        target=target,
        evaluators=(evaluator,),
        selection=selection,
        experiment_prefix=experiment_prefix,
        metadata={
            "dataset": dataset_name,
            "selection": selection,
            "evaluator_keys": list(ANSWER_EVALUATOR_KEYS),
        },
        title=ANSWER_EXPERIMENT_TITLE,
        keys=ANSWER_EVALUATOR_KEYS,
    )


#: 五个 key 的稳定顺序（写入实验 metadata，便于事后核对）
DEFAULT_EVALUATOR_KEYS: Final[tuple[str, ...]] = (
    HIT_AT_1_KEY,
    HIT_AT_K_KEY,
    PRECISION_KEY,
    RECALL_KEY,
    NEGATIVE_SAMPLE_KEY,
)


__all__ = [
    "ANSWER_EVALUATOR_KEYS",
    "ANSWER_EXPERIMENT_PREFIX",
    "ANSWER_EXPERIMENT_TITLE",
    "ANSWER_RELEVANCE_KEY",
    "ANSWER_SELECTION",
    "ANSWER_VERIFICATION_CASE_IDS",
    "DEFAULT_EVALUATOR_KEYS",
    "DEFAULT_EVALUATORS",
    "EVALUATION_K",
    "ExperimentSummary",
    "FULL_SELECTION",
    "HIT_AT_1_KEY",
    "HIT_AT_K_KEY",
    "Judgement",
    "MISSING_ANSWER_REASON",
    "NEGATIVE_SAMPLE_KEY",
    "PRECISION_KEY",
    "RECALL_KEY",
    "RETRIEVED_KEY",
    "RunAnswer",
    "SMOKE_SELECTION",
    "SOURCES_KEY",
    "RunRetrieval",
    "build_answer_relevance_evaluator",
    "build_answer_target",
    "build_case",
    "build_retrieval_target",
    "extract_run_answer",
    "extract_run_retrieval",
    "judge",
    "rag_hit_at_1",
    "rag_hit_at_k",
    "rag_negative_sample",
    "rag_precision",
    "rag_recall",
    "run_answer_relevance_evaluation",
    "run_retrieval_evaluation",
    "select_examples",
    "summarize_experiment",
]
