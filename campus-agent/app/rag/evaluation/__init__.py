"""RAG 离线评估包。

回答一个很具体的问题：

    给定一个问题，我期望召回哪些知识，当前 Retriever 实际召回了什么，结果是否正确？

三段式结构::

    dataset    数据集：JSON -> EvaluationCase 列表（含校验）
    evaluator  评估：question -> Retriever -> chunks -> 指标
    schema     契约：CaseResult / EvaluationReport（含序列化与报告渲染）

架构约束：本包**只**调用 ``KnowledgeRetriever``，不直接接触向量库、
不接触 RAGService / Agent / Graph，更不调用 LLM。
评估对象是检索质量，链路固定为 ``Evaluation -> Retriever -> VectorStore``。

用法::

    from app.rag.evaluation import evaluate_dataset, load_dataset

    report = evaluate_dataset(load_dataset())
    print(report.format_report())

离线测试时刻注入替身检索器，保证不触网::

    evaluate_dataset(cases, retriever=KnowledgeRetriever(FakeVectorStore(hits)))
"""

from app.rag.evaluation.dataset import (
    DEFAULT_DATASET_FILENAME,
    DatasetError,
    default_dataset_path,
    load_dataset,
    parse_cases,
    summarize_dataset,
)
from app.rag.evaluation.evaluator import (
    default_retriever,
    evaluate_case,
    evaluate_dataset,
    evaluate_retrieval,
    score_case,
)
from app.rag.evaluation.schema import (
    DEFAULT_HIT_K,
    CaseResult,
    EvaluationCase,
    EvaluationReport,
)

__all__ = [
    "DEFAULT_DATASET_FILENAME",
    "DEFAULT_HIT_K",
    "CaseResult",
    "DatasetError",
    "EvaluationCase",
    "EvaluationReport",
    "default_dataset_path",
    "default_retriever",
    "evaluate_case",
    "evaluate_dataset",
    "evaluate_retrieval",
    "load_dataset",
    "parse_cases",
    "score_case",
    "summarize_dataset",
]
