"""文档加载 → 清洗 → 切分 → Embedding → 向量库写入的统一索引流水线（支持增量）。

这是 RAG 链路中**唯一**允许同时接触加载器、切分器与向量库的地方；
其余模块各自只依赖相邻的一层，保证分层不被穿透。

增量索引的职责划分：

``incremental``  判定：目录 + 清单 -> 变更计划（纯计算，不碰向量库）
``manifest``     状态：记录每个 source 的摘要、片段数与片段 id
``pipeline``     执行：按计划删旧片段 / 写新片段 / 跳过 / 更新清单（本文件）

执行顺序与理由：

1. 先完成全部 NEW / CHANGED 文档的「加载 → 清洗 → 切分」，此阶段不触碰向量库；
   这样即使某个文件解析失败，也不会让向量库处于半删半写的状态。
2. 再统一清理旧片段（CHANGED 的旧版本 + DELETED 的全部），最后写入新片段。
   顺序为「先删后写」，保证同一 source 的新旧片段不会同时存在。
   CHANGED 仅在「新内容已成功读入」时才删旧片段：源文件读不出来时保留旧向量，
   好过让知识内容凭空消失（该文档会出现在报告的 SKIPPED 行中）。
3. 最后刷新清单，并重新统计库内片段数。

片段 id 由「来源 + 页码 + 片段序号 + 内容」派生：
同一份资料重复写入得到相同 id，向量库按 upsert 覆盖而非新增，
因此即便清单丢失（最坏情况整库被判定为 NEW）也不会产生重复向量。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config.settings import RAGSettings, get_rag_settings
from app.rag.incremental import (
    DocumentStatus,
    compute_index_signature,
    plan_changes,
)
from app.rag.loader import clean_documents, load_files
from app.rag.manifest import (
    DocumentRecord,
    ManifestStore,
    resolve_manifest_path,
    utc_now,
)
from app.rag.schema import IngestReport
from app.rag.splitter import create_text_splitter, split_documents
from app.rag.vectorstore import (
    count_documents,
    delete_chunks,
    get_vector_store,
    reset_collection,
)

logger = logging.getLogger(__name__)

#: 进程内索引状态：避免每次检索都重复检查知识库
_INGEST_LOCK: threading.Lock = threading.Lock()
_LAST_REPORT: IngestReport | None = None


def _chunk_id(chunk: Document) -> str:
    """由来源 + 序号 + 内容派生稳定 id。"""

    payload = "|".join(
        (
            str(chunk.metadata.get("source", "")),
            str(chunk.metadata.get("page", "")),
            str(chunk.metadata.get("chunk_index", "")),
            chunk.page_content,
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _source_of(document: Document) -> str:
    """取片段的来源标识（与清单的 key 保持一致）。"""

    return str(document.metadata.get("source", ""))


def ingest_directory(
    root: Path | str | None = None,
    *,
    store: VectorStore | None = None,
    splitter: RecursiveCharacterTextSplitter | None = None,
    settings: RAGSettings | None = None,
    full: bool = False,
) -> IngestReport:
    """让向量库与知识库目录对齐（默认增量，``full=True`` 时全量重建）。

    Args:
        root: 知识库目录；省略时取配置中的 ``knowledge_dir``。
        store: 目标向量库；省略时使用全局共享实例。
        splitter: 切分器；省略时按生效配置构建。
        settings: RAG 配置；省略时读取全局配置。
        full: 是否先清空集合再做全量重建。``False``（默认）时只处理
            NEW / CHANGED / DELETED 三类文档，UNCHANGED 不重新 embedding。

    Returns:
        本次索引的统计结果。
    """

    rag_settings = settings or get_rag_settings()
    knowledge_dir = Path(root) if root is not None else rag_settings.knowledge_dir
    active_store = store or get_vector_store()

    manifest_store = ManifestStore(resolve_manifest_path(rag_settings))
    manifest = manifest_store.load()
    signature = compute_index_signature(rag_settings)

    # 向量库被清空而清单仍在（例如手工删掉 .storage/vectorstore）：
    # 此时若仍按清单判为 UNCHANGED，知识库会永久为空，因此强制重新索引。
    existing = count_documents(active_store)
    store_missing = existing == 0 and manifest.indexed_count > 0

    if full:
        reset_collection(active_store)

    plan = plan_changes(
        knowledge_dir,
        manifest,
        signature=signature,
        rebuild=full,
        force_reindex=store_missing,
    )

    # 1) DELETED 的旧片段无论其源文件能否读取都必须清理
    #    （全量重建时集合已整体清空，无需再按 source 清理）
    stale_ids: list[str] = (
        [] if full else list(plan.stale_chunk_ids(DocumentStatus.DELETED))
    )

    # 2) NEW / CHANGED：先完成「加载 → 清洗 → 切分」，全程不触碰向量库
    pending = plan.pending()
    raw_documents, load_skipped = load_files(
        knowledge_dir, [change.path for change in pending if change.path is not None]
    )
    loaded_sources = {_source_of(document) for document in raw_documents}
    documents = clean_documents(raw_documents)
    chunks = split_documents(documents, splitter or create_text_splitter(rag_settings))

    # CHANGED 仅在「新内容已成功读入」时才删除旧片段
    stale_ids.extend(
        plan.stale_chunk_ids(DocumentStatus.CHANGED, only_sources=loaded_sources)
    )

    # 3) 先删后写，保证同一 source 的新旧片段不同时存在
    removed_chunks = delete_chunks(active_store, stale_ids)

    # 4) 写入新片段（Embedding 由向量库内部按 embedding_function 调用）
    chunk_ids = [_chunk_id(chunk) for chunk in chunks]
    if chunks:
        active_store.add_documents(chunks, ids=chunk_ids)

    # 5) 按 source 归集本次写入的片段 id，刷新清单
    written: dict[str, list[str]] = {}
    for chunk, chunk_id in zip(chunks, chunk_ids):
        written.setdefault(_source_of(chunk), []).append(chunk_id)

    indexed_at = utc_now()
    upserts: list[DocumentRecord] = []
    for change in pending:
        if change.source not in loaded_sources:
            # 读取失败：保留清单中的旧记录，下一次仍会被判为 NEW / CHANGED
            continue
        source_ids = written.get(change.source, [])
        upserts.append(
            DocumentRecord(
                source=change.source,
                content_hash=change.content_hash,
                indexed_at=indexed_at,
                chunk_count=len(source_ids),
                chunk_ids=tuple(source_ids),
            )
        )

    updated = manifest.evolve(
        upserts=upserts,
        removed=[change.source for change in plan.select(DocumentStatus.DELETED)],
        signature=signature,
        updated_at=indexed_at,
    )
    manifest_store.save(updated)

    total = count_documents(active_store)
    report = IngestReport(
        documents=len(upserts),
        chunks=len(chunks),
        total_chunks=total,
        new=plan.count(DocumentStatus.NEW),
        changed=plan.count(DocumentStatus.CHANGED),
        unchanged=plan.count(DocumentStatus.UNCHANGED),
        deleted=plan.count(DocumentStatus.DELETED),
        removed_chunks=removed_chunks,
        skipped=tuple(plan.skipped) + tuple(load_skipped),
        rebuilt=full,
        performed=True,
    )
    logger.info("%s", report.describe())
    return report


def ensure_knowledge_base(
    *,
    settings: RAGSettings | None = None,
    full: bool = False,
    store: VectorStore | None = None,
) -> IngestReport:
    """确保知识库已就绪；仅在集合为空（或要求重建）时执行索引。

    面向检索链路的自动兜底：正常启动时知识库已有内容，此处直接跳过，
    因此不会每次提问都扫描知识目录。知识库的日常更新走 ``--ingest``。

    Args:
        settings: RAG 配置；省略时读取全局配置。
        full: 强制全量重建。
        store: 目标向量库；省略时使用全局共享实例。
    """

    global _LAST_REPORT

    with _INGEST_LOCK:
        if not full and _LAST_REPORT is not None and not _LAST_REPORT.rebuilt:
            return _LAST_REPORT

        rag_settings = settings or get_rag_settings()
        active_store = store or get_vector_store()
        existing = count_documents(active_store)

        if not full and existing > 0:
            logger.info("知识库已就绪，跳过索引（库内片段 %d 个）", existing)
            _LAST_REPORT = IngestReport(total_chunks=existing, performed=False)
            return _LAST_REPORT

        if not full and not rag_settings.auto_ingest:
            logger.warning("知识库为空且 RAG_AUTO_INGEST=false，需手动执行 --ingest")
            _LAST_REPORT = IngestReport(total_chunks=existing, performed=False)
            return _LAST_REPORT

        if not full and existing == 0:
            logger.info("知识库为空，开始首次索引: %s", rag_settings.knowledge_dir)

        _LAST_REPORT = ingest_directory(
            store=active_store, settings=rag_settings, full=full
        )
        return _LAST_REPORT


def reset_ingest_state() -> None:
    """清除进程内索引缓存（测试与显式重建时使用）。"""

    global _LAST_REPORT
    with _INGEST_LOCK:
        _LAST_REPORT = None


__all__ = ["ensure_knowledge_base", "ingest_directory", "reset_ingest_state"]
