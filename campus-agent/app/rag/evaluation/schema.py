"""RAG 离线评估的数据契约。

本模块只定义「一条用例」与「一份报告」的字段，不含任何检索与计算逻辑，
因此可以被数据集加载、评估计算与 CLI 输出三方共享。

指标定义（口径固定在此，避免各处理解不一致）::

    hit@1        Top 1 片段的 source 是否命中 expected_sources
    hit@k        Top K 片段（按 source 去重后）是否至少命中一个 expected_sources
    precision    去重后的实际 source 中，属于 expected_sources 的比例
    recall       expected_sources 中被召回的比例

两类特殊用例的口径：

- **负样本**（``expected_sources`` 为空，即「本就不该命中知识库」的问题）：
  没有「正确的 source」，precision / recall **未定义**（记为 ``None``），
  hit 记为 ``False``，且**不参与**任何汇总均值，只在报告中单独计数。
- **零召回**（有期望来源但一条都没搜到）：precision / recall 均记为 ``0.0``。
  把「什么都没返回」视为「没有任何正确结果」，使均值始终有定义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: hit@k 的默认 k 值。既是召回深度，也是判定窗口。
DEFAULT_HIT_K: int = 3


def _percent(value: float) -> str:
    """把 ``[0, 1]`` 的比例渲染为百分比文本。"""

    return f"{value:.1%}"


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一条检索评估用例。"""

    question: str
    #: 期望命中的来源文件（相对知识库根目录的 posix 路径）；为空表示负样本
    expected_sources: tuple[str, ...] = ()
    #: 期望出现在片段内容中的关键词（可选，仅作诊断记录，不参与指标计算）
    expected_keywords: tuple[str, ...] = ()
    #: 期望的最低相关性分数（可选，仅作诊断记录，不参与指标计算）
    expected_min_score: float | None = None
    #: 用例标识，缺省时用问题原文代替
    case_id: str = ""

    @property
    def is_negative(self) -> bool:
        """是否负样本：期望「不命中任何知识库来源」的问题。"""

        return not self.expected_sources

    @property
    def label(self) -> str:
        """报告中展示用的用例名。"""

        return self.case_id or self.question

    def as_dict(self) -> dict[str, Any]:
        """转换为可序列化结构（与数据文件字段一一对应）。"""

        return {
            "question": self.question,
            "expected_sources": list(self.expected_sources),
            "expected_keywords": list(self.expected_keywords),
            "expected_min_score": self.expected_min_score,
            "case_id": self.case_id,
        }


@dataclass(frozen=True, slots=True)
class CaseResult:
    """单条用例的评估结果。"""

    question: str
    expected_sources: tuple[str, ...]
    #: 实际命中的来源，按相关性降序且已按 source 去重
    actual_sources: tuple[str, ...]
    #: 每条召回片段的相关性分数，与召回顺序一一对应（未去重）
    scores: tuple[float | None, ...]
    hit_at_1: bool
    hit_at_k: bool
    #: 实际 source 中命中期望的比例；负样本为 ``None``（未定义）
    precision: float | None
    #: 期望 source 被召回的比例；负样本为 ``None``（未定义）
    recall: float | None
    #: 本次实际召回的片段条数
    retrieved: int
    #: 首个命中期望来源的排名（1 起）；未命中为 ``None``
    first_relevant_rank: int | None
    #: 本次判定使用的 k
    k: int
    case_id: str = ""
    #: 用例声明的期望关键词（快照，便于对照报告理解失败原因）
    expected_keywords: tuple[str, ...] = ()
    #: 召回片段正文中真实出现的关键词；用于诊断「文档对了但段落错了」
    matched_keywords: tuple[str, ...] = ()
    #: 是否达到用例声明的 ``expected_min_score``；未声明时为 ``None``。
    #: 这是分数标定的辅助诊断，与 hit / precision / recall 一样**不改变**召回结果。
    min_score_ok: bool | None = None

    @property
    def is_negative(self) -> bool:
        """是否负样本。"""

        return not self.expected_sources

    @property
    def passed(self) -> bool:
        """是否通过（以 hit@k 为准）。"""

        return self.hit_at_k

    @property
    def label(self) -> str:
        """报告中展示用的用例名。"""

        return self.case_id or self.question

    @property
    def matched_sources(self) -> tuple[str, ...]:
        """命中的期望来源（按召回顺序）。"""

        expected = set(self.expected_sources)
        return tuple(source for source in self.actual_sources if source in expected)

    @property
    def missed_sources(self) -> tuple[str, ...]:
        """未被召回的期望来源（按数据集声明顺序）。"""

        actual = set(self.actual_sources)
        return tuple(source for source in self.expected_sources if source not in actual)

    def as_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""

        return {
            "case_id": self.case_id,
            "question": self.question,
            "expected_sources": list(self.expected_sources),
            "actual_sources": list(self.actual_sources),
            "scores": list(self.scores),
            "hit_at_1": self.hit_at_1,
            "hit_at_k": self.hit_at_k,
            "precision": self.precision,
            "recall": self.recall,
            "retrieved": self.retrieved,
            "first_relevant_rank": self.first_relevant_rank,
            "k": self.k,
            "expected_keywords": list(self.expected_keywords),
            "matched_keywords": list(self.matched_keywords),
            "min_score_ok": self.min_score_ok,
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """一次数据集评估的汇总报告。

    汇总指标（hit@1 / hit@k / precision / recall）**只在正样本上取均值**：
    负样本没有「正确答案」，混入均值会让数字失去意义，
    因此它们只通过 ``no_expected_source_cases`` 与 ``unexpected_hits`` 单独呈现。
    """

    k: int
    total_cases: int
    hit_at_1: float
    hit_at_k: float
    precision: float
    recall: float
    no_expected_source_cases: int
    no_hit_cases: int
    failed_cases: tuple[CaseResult, ...]
    cases: tuple[CaseResult, ...] = ()
    #: 负样本中实际召回了片段的条数（越高说明「本不该命中」的问题被误召回得越多）
    unexpected_hits: int = 0
    #: 声明了 ``expected_min_score`` 的用例数
    min_score_cases: int = 0
    #: 其中未达到声明下限的条数（分数标定的回归信号）
    min_score_failures: int = 0

    @property
    def scored_cases(self) -> int:
        """参与汇总的用例数（正样本数）。"""

        return self.total_cases - self.no_expected_source_cases

    @property
    def retrieved_cases(self) -> int:
        """有召回的用例数（含负样本）；为 0 通常意味着知识库尚未索引。"""

        return sum(1 for case in self.cases if case.retrieved > 0)

    @property
    def passed(self) -> bool:
        """是否全部正样本都通过 hit@k。"""

        return self.no_hit_cases == 0

    def as_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""

        return {
            "k": self.k,
            "total_cases": self.total_cases,
            "scored_cases": self.scored_cases,
            "hit_at_1": self.hit_at_1,
            "hit_at_k": self.hit_at_k,
            "precision": self.precision,
            "recall": self.recall,
            "no_expected_source_cases": self.no_expected_source_cases,
            "unexpected_hits": self.unexpected_hits,
            "min_score_cases": self.min_score_cases,
            "min_score_failures": self.min_score_failures,
            "no_hit_cases": self.no_hit_cases,
            "failed_cases": [case.as_dict() for case in self.failed_cases],
            "cases": [case.as_dict() for case in self.cases],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """序列化为 JSON 文本（``ensure_ascii=False``，便于人工查看中文）。"""

        return json.dumps(self.as_dict(), ensure_ascii=False, indent=indent)

    def format_report(self) -> str:
        """返回命令行展示用的评估报告。

        前五行是固定指标（便于脚本按行解析），随后按需追加汇总补充与失败明细。
        """

        lines = [
            "RAG Evaluation",
            "--------------",
            f"Cases: {self.total_cases}",
            f"Hit@1: {_percent(self.hit_at_1)}",
            f"Hit@{self.k}: {_percent(self.hit_at_k)}",
            f"Precision: {_percent(self.precision)}",
            f"Recall: {_percent(self.recall)}",
        ]

        summary = [
            f"正样本: {self.scored_cases}（负样本 {self.no_expected_source_cases}）",
            f"未命中: {self.no_hit_cases}",
        ]
        if self.no_expected_source_cases:
            summary.append(
                f"负样本误召回: {self.unexpected_hits}/{self.no_expected_source_cases}"
            )
        if self.min_score_cases:
            summary.append(
                f"分数标定: {self.min_score_cases - self.min_score_failures}/"
                f"{self.min_score_cases} 达到声明下限"
            )
        lines.append("")
        lines.extend(summary)

        if self.failed_cases:
            lines.append("")
            lines.append("Failed cases（未能命中期望来源）:")
            lines.extend(
                _format_case_detail(case, index) for index, case in enumerate(self.failed_cases, 1)
            )

        negatives = [case for case in self.cases if case.is_negative]
        if negatives:
            lines.append("")
            lines.append("No-expected-source cases（期望不命中明确知识）:")
            for index, case in enumerate(negatives, 1):
                actual = ", ".join(case.actual_sources) or "(无召回)"
                lines.append(f"  [{index}] {case.label}")
                lines.append(f"      actual: {actual}")

        return "\n".join(lines)


def _format_case_detail(case: CaseResult, index: int) -> str:
    """渲染单条失败用例的明细。"""

    lines = [
        f"  [{index}] {case.label}",
        f"      expected: {', '.join(case.expected_sources)}",
        f"      actual:   {_format_scores(case.actual_sources, case.scores)}",
    ]
    missed = case.missed_sources
    if missed:
        lines.append(f"      missed:   {', '.join(missed)}")
    if case.expected_keywords:
        hit_total = len(case.matched_keywords)
        lines.append(
            f"      keywords: {hit_total}/{len(case.expected_keywords)}"
            f"（{', '.join(case.matched_keywords) or '无'}）"
        )
    if case.min_score_ok is False:
        lines.append("      min_score: 未达到用例声明的分数下限")
    if case.retrieved == 0:
        lines.append("      （本次无任何召回）")
    return "\n".join(lines)


def _format_scores(sources: tuple[str, ...], scores: tuple[float | None, ...]) -> str:
    """把「来源 + 分数」渲染成 ``a.md (0.910), b.md (0.420)`` 形式。

    ``scores`` 与 ``sources`` 一一对应（见 ``CaseResult.scores`` 的说明），
    因此直接按位配对即可。
    """

    if not sources:
        return "(无召回)"

    rendered = []
    for source, score in zip(sources, scores):
        rendered.append(f"{source} ({score:.3f})" if score is not None else f"{source} (-)")
    return ", ".join(rendered)


__all__ = [
    "DEFAULT_HIT_K",
    "CaseResult",
    "EvaluationCase",
    "EvaluationReport",
]
