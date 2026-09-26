"""评估数据集的读取与校验。

数据集用最朴素的 JSON 描述（``data/evaluation/retrieval_cases.json``），
每条用例至少包含 ``question`` 与 ``expected_sources``：

.. code-block:: json

    {
      "version": 1,
      "cases": [
        {
          "question": "图书馆开放时间是怎么规定的？",
          "expected_sources": ["campus_rules.md"]
        }
      ]
    }

``expected_sources`` 为空数组表示**负样本**：这个问题本就不该命中知识库，
因此不参与 precision / recall 汇总（口径见 ``schema`` 模块）。

刻意不做的事：不支持 YAML、不支持远程拉取、不做自动生成。
数据集是人工维护的「标准答案」，任何花哨格式都只会增加维护成本。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.config.settings import app_root
from app.rag.evaluation.schema import EvaluationCase

#: 默认数据集文件名
DEFAULT_DATASET_FILENAME: str = "retrieval_cases.json"


class DatasetError(ValueError):
    """评估数据集缺失或格式非法。"""


def default_dataset_path() -> Path:
    """返回默认数据集路径（相对应用根目录）。"""

    return app_root() / "data" / "evaluation" / DEFAULT_DATASET_FILENAME


def _as_str_tuple(value: Any, *, field: str, case_label: str) -> tuple[str, ...]:
    """把 JSON 字段规整为去空、去重且保持原序的字符串元组。"""

    if value is None:
        return ()
    if isinstance(value, str):
        raise DatasetError(f"用例 {case_label!r} 的 {field} 必须是数组，而不是单个字符串")
    if not isinstance(value, list):
        raise DatasetError(f"用例 {case_label!r} 的 {field} 必须是数组")

    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DatasetError(f"用例 {case_label!r} 的 {field} 只能包含字符串，当前: {item!r}")
        text = item.strip()
        if not text:
            raise DatasetError(f"用例 {case_label!r} 的 {field} 不能包含空字符串")
        if text not in seen:
            seen.append(text)
    return tuple(seen)


def _parse_case(payload: Any, index: int) -> EvaluationCase:
    """把一条原始 JSON 记录解析为 ``EvaluationCase``。"""

    if not isinstance(payload, dict):
        raise DatasetError(f"第 {index + 1} 条用例必须是对象，当前: {type(payload).__name__}")

    raw_question = payload.get("question")
    if not isinstance(raw_question, str) or not raw_question.strip():
        raise DatasetError(f"第 {index + 1} 条用例缺少非空的 question")

    question = raw_question.strip()
    case_id = payload.get("case_id") or ""
    if not isinstance(case_id, str):
        raise DatasetError(f"用例 {question!r} 的 case_id 必须是字符串")
    label = case_id.strip() or question

    min_score = payload.get("expected_min_score")
    if min_score is not None:
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
            raise DatasetError(f"用例 {label!r} 的 expected_min_score 必须是数字")
        if not 0.0 <= float(min_score) <= 1.0:
            raise DatasetError(
                f"用例 {label!r} 的 expected_min_score 必须落在 [0, 1]，当前: {min_score}"
            )
        min_score = float(min_score)

    return EvaluationCase(
        question=question,
        expected_sources=_as_str_tuple(
            payload.get("expected_sources", []), field="expected_sources", case_label=label
        ),
        expected_keywords=_as_str_tuple(
            payload.get("expected_keywords", []), field="expected_keywords", case_label=label
        ),
        expected_min_score=min_score,
        case_id=case_id.strip(),
    )


def parse_cases(payload: Any) -> list[EvaluationCase]:
    """解析数据集内容。

    Args:
        payload: 已反序列化的 JSON 内容，既可以是 ``{"cases": [...]}``，
            也可以直接是用例数组。

    Raises:
        DatasetError: 结构非法、必填字段缺失或存在重复问题。
    """

    if isinstance(payload, list):
        raw_cases: Any = payload
    elif isinstance(payload, dict):
        raw_cases = payload.get("cases")
        if raw_cases is None:
            raise DatasetError("数据集缺少 cases 字段")
    else:
        raise DatasetError(f"数据集根节点必须是对象或数组，当前: {type(payload).__name__}")

    if not isinstance(raw_cases, list):
        raise DatasetError("cases 必须是数组")
    if not raw_cases:
        raise DatasetError("评估数据集为空，至少需要一条用例")

    cases = [_parse_case(item, index) for index, item in enumerate(raw_cases)]

    questions = [case.question for case in cases]
    duplicated = {question for question in questions if questions.count(question) > 1}
    if duplicated:
        # 重复问题会让同一行为被重复计权，属于典型的数据集维护事故，直接拒绝
        raise DatasetError(f"数据集中存在重复问题: {', '.join(sorted(duplicated))}")

    return cases


def load_dataset(path: Path | str | None = None) -> list[EvaluationCase]:
    """从文件加载评估数据集。

    Args:
        path: 数据集路径；省略时使用 ``data/evaluation/retrieval_cases.json``。

    Raises:
        DatasetError: 文件缺失、JSON 非法或结构不符合约定。
    """

    dataset_path = Path(path) if path is not None else default_dataset_path()
    if not dataset_path.is_file():
        raise DatasetError(f"评估数据集不存在: {dataset_path}")

    try:
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"评估数据集不是合法 JSON（{dataset_path}）: {exc}") from exc

    return parse_cases(payload)


def summarize_dataset(cases: Iterable[EvaluationCase]) -> str:
    """返回数据集构成摘要，便于日志与报告展示。"""

    items = list(cases)
    negatives = sum(1 for case in items if case.is_negative)
    sources = sorted({source for case in items for source in case.expected_sources})
    return (
        f"用例 {len(items)} 条（负样本 {negatives} 条），"
        f"覆盖来源 {len(sources)} 个: {', '.join(sources) or '(无)'}"
    )


__all__ = [
    "DEFAULT_DATASET_FILENAME",
    "DatasetError",
    "default_dataset_path",
    "load_dataset",
    "parse_cases",
    "summarize_dataset",
]
