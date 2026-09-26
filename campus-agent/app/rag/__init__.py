"""RAG 基础设施包。

分层与依赖方向（上层依赖下层，禁止反向依赖）::

    pipeline     编排：加载 -> 清洗 -> 切分 -> 向量化写入（唯一跨层编排点）
    incremental  增量判定：目录 + 清单 -> 变更计划（纯计算，不碰向量库）
    manifest     索引状态：记录每个 source 的摘要、片段数与片段 id
    evaluation   离线评估：数据集 -> Retriever -> 指标（只读，不写向量库）
    service      RAGService.retrieve / answer——Agent 层唯一门面
    retriever    把查询变成带来源的片段，屏蔽向量库细节
    vectorstore  向量库工厂（chroma 现役，milvus 预留登记位）
    embeddings   向量化模型工厂（DashScope / OpenAI 兼容协议）
    splitter     统一切分策略（参数来自配置）
    loader       多格式文档加载与清洗

Agent 层（``app/agent``）只允许依赖 ``service`` 与本包的 ``schema``，
不得直接接触 ``vectorstore`` / ``embeddings``，以保证向量库可替换。
``evaluation`` 横跨在 ``service`` 之外，只依赖 ``retriever``：
评估对象是检索质量本身，不应被 Agent 与模型影响。
"""

from app.rag.evaluation import (
    CaseResult,
    DatasetError,
    EvaluationCase,
    EvaluationReport,
    default_dataset_path,
    evaluate_dataset,
    load_dataset,
    score_case,
    summarize_dataset,
)
from app.rag.incremental import (
    ChangePlan,
    DocumentChange,
    DocumentStatus,
    compute_index_signature,
    hash_file,
    plan_changes,
)
from app.rag.manifest import (
    MANIFEST_FILENAME,
    DocumentRecord,
    IndexManifest,
    ManifestStore,
    resolve_manifest_path,
)
from app.rag.pipeline import ensure_knowledge_base, ingest_directory, reset_ingest_state
from app.rag.schema import IngestReport, RAGAnswer, RetrievedChunk
from app.rag.service import RAGService, build_rag_service, get_rag_service

__all__ = [
    "MANIFEST_FILENAME",
    "CaseResult",
    "ChangePlan",
    "DatasetError",
    "DocumentChange",
    "DocumentRecord",
    "DocumentStatus",
    "EvaluationCase",
    "EvaluationReport",
    "IndexManifest",
    "IngestReport",
    "ManifestStore",
    "RAGAnswer",
    "RAGService",
    "RetrievedChunk",
    "build_rag_service",
    "compute_index_signature",
    "default_dataset_path",
    "ensure_knowledge_base",
    "evaluate_dataset",
    "get_rag_service",
    "hash_file",
    "ingest_directory",
    "load_dataset",
    "plan_changes",
    "reset_ingest_state",
    "resolve_manifest_path",
    "score_case",
    "summarize_dataset",
]
