"""检索评估器：把「问题 → 期望来源」与「Retriever 实际召回」对齐打分。

依赖方向（本阶段的核心架构约束）::

    Evaluation → Retriever → VectorStore

本模块**只**依赖 ``KnowledgeRetriever`` 这一层，不接触向量库、不接触 Embedding、
更不接触 Agent / Graph / LLM。原因：

1. 评估对象是**检索质量**，不是生成质量；引入 LLM 会让结果随温度、版本、网络抖动
   变化，评估必须可重复。
2. 只调 Retriever 意味着「换向量库 / 加混合检索 / 加重排序」后，
   同一份数据集可以直接对比前后得分——这正是本套基础设施存在的意义。

因此链路固定为::

    question → Retriever → chunks → 指标计算

而不是 ``question → Agent → LLM → 评估``。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Final

from app.rag.evaluation.schema import (
    DEFAULT_HIT_K,
    CaseResult,
    EvaluationCase,
    EvaluationReport,
)
from app.rag.retriever import KnowledgeRetriever
from app.rag.schema import RetrievedChunk

logger = logging.getLogger(__name__)

#: 未指定来源时的占位标识（与检索层保持一致，避免把未知来源误判为命中）
_UNKNOWN_SOURCE: Final[str] = "未知来源"


def default_retriever() -> KnowledgeRetriever:
    """按当前配置构建检索器（评估入口的唯一装配点）。

    刻意放在函数内按需构建，且**只在真正需要检索时**才被调用：
    导入本模块不读取配置、不建连接，测试可全程注入替身。
    """

    return KnowledgeRetriever.from_settings()


def _unique_sources(chunks: Sequence[RetrievedChunk]) -> tuple[tuple[str, ...], tuple[float | None, ...]]:
    """把召回片段按来源去重，保留「首次出现（即最高分）」的分数。

    指标在 **source 粒度**上计算（一个文档可能被切成很多片段，
    多召回几个同源片段不该让 precision 变好看），因此这里做去重。
    返回的两个元组长度相等且一一对应，可直接 zip。
    """

    scores: dict[str, float | None] = {}
    for chunk in chunks:
        source = chunk.source or _UNKNOWN_SOURCE
        if source not in scores:
            scores[source] = chunk.score
    return tuple(scores), tuple(scores.values())


def score_case(case: EvaluationCase, chunks: Sequence[RetrievedChunk], *, k: int) -> CaseResult:
    """用已召回的片段给单条用例打分（纯计算，不触发检索）。

    把「检索」与「打分」分开的好处：指标口径可以脱离向量库独立验证，
    也便于将来对同一份召回结果做多种口径的离线比较。
    """

    top = list(chunks)[:k]
    actual_sources, scores = _unique_sources(top)
    matched_keywords = _match_keywords(top, case.expected_keywords)

    expected = tuple(case.expected_sources)
    if not expected:
        # 负样本：没有「正确答案」，hit 无意义、precision / recall 未定义
        return CaseResult(
            question=case.question,
            expected_sources=expected,
            actual_sources=actual_sources,
            scores=scores,
            hit_at_1=False,
            hit_at_k=False,
            precision=None,
            recall=None,
            retrieved=len(top),
            first_relevant_rank=None,
            k=k,
            case_id=case.case_id,
            expected_keywords=tuple(case.expected_keywords),
            matched_keywords=matched_keywords,
        )

    expected_set = set(expected)
    matched = [source for source in actual_sources if source in expected_set]
    first_rank = next(
        (rank for rank, source in enumerate(actual_sources, 1) if source in expected_set),
        None,
    )
    # 无召回时刻意记为 0.0（而不是未定义）：把「什么都没返回」视为「没有任何正确结果」，
    # 这样汇总均值始终有定义，也不会把一次彻底失败"隐身"掉。
    precision = len(matched) / len(actual_sources) if actual_sources else 0.0
    recall = len(matched) / len(expected)

    if _UNKNOWN_SOURCE in actual_sources:
        logger.debug("用例 %r 的召回结果包含缺少 source 的片段", case.label)

    return CaseResult(
        question=case.question,
        expected_sources=expected,
        actual_sources=actual_sources,
        scores=scores,
        hit_at_1=bool(actual_sources[:1] and actual_sources[0] in expected_set),
        hit_at_k=bool(matched),
        precision=precision,
        recall=recall,
        retrieved=len(top),
        first_relevant_rank=first_rank,
        k=k,
        case_id=case.case_id,
        expected_keywords=tuple(case.expected_keywords),
        matched_keywords=matched_keywords,
        min_score_ok=_check_min_score(case, actual_sources, scores, expected_set),
    )


def _match_keywords(
    chunks: Sequence[RetrievedChunk], keywords: Sequence[str]
) -> tuple[str, ...]:
    """统计期望关键词在召回片段正文中的出现情况（辅助诊断，不参与指标）。"""

    if not keywords:
        return ()
    text = "\n".join(chunk.content for chunk in chunks)
    return tuple(keyword for keyword in keywords if keyword in text)


def _check_min_score(
    case: EvaluationCase,
    actual_sources: Sequence[str],
    scores: Sequence[float | None],
    expected_set: set[str],
) -> bool | None:
    """检查命中来源的相关性是否达到用例声明的下限。

    用途是**分数标定**：若某条用例长期「source 命中但分数偏低」，
    说明向量库的距离换算或阈值设置需要复核。未声明下限时返回 ``None``。
    """

    if case.expected_min_score is None:
        return None

    best = [
        score
        for source, score in zip(actual_sources, scores)
        if source in expected_set and score is not None
    ]
    if not best:
        return False
    return max(best) >= case.expected_min_score


def evaluate_retrieval(
    question: str,
    expected_sources: Sequence[str],
    *,
    retriever: KnowledgeRetriever | None = None,
    top_k: int = DEFAULT_HIT_K,
    expected_keywords: Sequence[str] = (),
    expected_min_score: float | None = None,
    case_id: str = "",
) -> CaseResult:
    """评估单个问题的检索结果（``question → Retriever → 指标``）。

    Args:
        question: 用户问题。
        expected_sources: 期望命中的来源文件；为空表示负样本（见 ``schema``）。
        retriever: 检索器；省略时按当前配置构建。测试务必注入替身以保证离线。
        top_k: 召回深度，同时也是 hit@k 的 k。

    Returns:
        单条用例的评估结果。
    """

    case = EvaluationCase(
        question=question,
        expected_sources=tuple(expected_sources),
        expected_keywords=tuple(expected_keywords),
        expected_min_score=expected_min_score,
        case_id=case_id,
    )
    return evaluate_case(case, retriever=retriever, top_k=top_k)


def evaluate_case(
    case: EvaluationCase,
    *,
    retriever: KnowledgeRetriever | None = None,
    top_k: int = DEFAULT_HIT_K,
) -> CaseResult:
    """检索并评估单条用例。"""

    active = retriever or default_retriever()
    chunks = active.retrieve(case.question, top_k=top_k)
    return score_case(case, chunks, k=top_k)


def evaluate_dataset(
    cases: Iterable[EvaluationCase],
    *,
    retriever: KnowledgeRetriever | None = None,
    top_k: int = DEFAULT_HIT_K,
) -> EvaluationReport:
    """评估整个数据集并汇总报告。

    Args:
        cases: 用例集合（通常来自 ``dataset.load_dataset``）。
        retriever: 检索器；省略时按当前配置构建。**整份数据集复用同一个实例**，
            避免每个问题都重建向量库连接。
        top_k: 每条用例的召回深度，同时也是 hit@k 的 k。

    Returns:
        ``EvaluationReport``：汇总指标 + 每条用例明细 + 失败用例清单。
    """

    items = list(cases)
    if not items:
        raise ValueError("评估数据集为空，无法评估")

    active = retriever or default_retriever()
    results: list[CaseResult] = []

    for case in items:
        result = evaluate_case(case, retriever=active, top_k=top_k)
        results.append(result)
        logger.debug(
            "评估: %s | hit@1=%s hit@%d=%s precision=%s recall=%s",
            result.label,
            result.hit_at_1,
            top_k,
            result.hit_at_k,
            result.precision,
            result.recall,
        )

    report = _summarize(results, k=top_k)
    logger.info(
        "检索评估完成: 用例 %d（正样本 %d）hit@1=%.1f%% hit@%d=%.1f%% precision=%.1f%% recall=%.1f%%",
        report.total_cases,
        report.scored_cases,
        report.hit_at_1 * 100,
        report.k,
        report.hit_at_k * 100,
        report.precision * 100,
        report.recall * 100,
    )
    return report


def _summarize(results: Sequence[CaseResult], *, k: int) -> EvaluationReport:
    """把逐条结果汇总为报告。

    汇总只在正样本上进行：负样本没有正确答案，precision / recall 为 ``None``，
    若强行按 0 计入会凭空拉低所有指标。
    """

    scored = [result for result in results if not result.is_negative]
    negatives = [result for result in results if result.is_negative]

    hit_at_1 = _mean([1.0 if result.hit_at_1 else 0.0 for result in scored])
    hit_at_k = _mean([1.0 if result.hit_at_k else 0.0 for result in scored])
    precision = _mean([float(result.precision) for result in scored if result.precision is not None])
    recall = _mean([float(result.recall) for result in scored if result.recall is not None])

    failed = tuple(result for result in scored if not result.hit_at_k)
    checked = [result for result in scored if result.min_score_ok is not None]

    return EvaluationReport(
        k=k,
        total_cases=len(results),
        hit_at_1=hit_at_1,
        hit_at_k=hit_at_k,
        precision=precision,
        recall=recall,
        no_expected_source_cases=len(negatives),
        no_hit_cases=len(failed),
        failed_cases=failed,
        cases=tuple(results),
        unexpected_hits=sum(1 for result in negatives if result.retrieved > 0),
        min_score_cases=len(checked),
        min_score_failures=sum(1 for result in checked if not result.min_score_ok),
    )


def _mean(values: Sequence[float]) -> float:
    """取均值；空序列返回 ``0.0``（无样本时指标视为 0，而非未定义或崩溃）。"""

    if not values:
        return 0.0
    return sum(values) / len(values)


__all__ = [
    "default_retriever",
    "evaluate_case",
    "evaluate_dataset",
    "evaluate_retrieval",
    "score_case",
]
