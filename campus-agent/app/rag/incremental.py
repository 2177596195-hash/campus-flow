"""增量索引的差异计算：把「知识库目录 + 索引清单」变成一份变更计划。

本模块只做判定，不做加载 / 切分 / 向量化，也不接触向量库，
因此可以完全离线单测，且与具体向量库后端无关。

判定规则：

===========  ==========================================  ==========================
状态          条件                                         后续动作（由 pipeline 执行）
===========  ==========================================  ==========================
``NEW``       清单中无该 source                             写入其全部片段
``CHANGED``   清单中的 ``content_hash`` 与当前文件不一致      先删旧片段，再写新片段
``UNCHANGED`` ``content_hash`` 一致                        跳过（不重新 embedding）
``DELETED``   清单中有、知识目录已无此文件                   删除其全部片段并移出清单
===========  ==========================================  ==========================

另有两条强制重新索引的规则：索引签名变化（切分 / Embedding 配置变了，
历史片段不可复用），以及向量库为空但清单非空（清库后必须补齐）。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from app.config.settings import RAGSettings
from app.rag.loader import iter_document_files
from app.rag.manifest import DocumentRecord, IndexManifest

logger = logging.getLogger(__name__)

#: 索引签名版本：参与签名的因素含义变化时递增，可强制全体重新索引
SIGNATURE_VERSION: Final[str] = "v1"
#: 计算文件摘要时的读取块大小
_HASH_CHUNK_SIZE: Final[int] = 64 * 1024


class DocumentStatus(StrEnum):
    """源文档相对索引清单的状态。"""

    #: 从未索引
    NEW = "NEW"
    #: 内容已变化，需要先删除旧片段再写入新片段
    CHANGED = "CHANGED"
    #: 内容未变化，跳过
    UNCHANGED = "UNCHANGED"
    #: 清单中存在，但知识目录已无此文件
    DELETED = "DELETED"


#: 需要重新写入向量的状态（NEW 与 CHANGED）
PENDING_STATUSES: Final[frozenset[DocumentStatus]] = frozenset(
    {DocumentStatus.NEW, DocumentStatus.CHANGED}
)


def hash_file(path: Path) -> str:
    """计算文件内容的 sha256。

    摘要基于**原始字节**而非抽取后的文本：无需解析即可判定，
    且任何字节级改动都能被感知，不会因加载器行为差异而漏判。
    """

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def compute_index_signature(settings: RAGSettings) -> str:
    """计算索引签名：影响「片段内容与向量语义」的配置指纹。

    切分参数、Embedding 模型或向量集合变化后，库里已有的片段与新配置不再匹配，
    因此签名不一致时必须重新索引，避免新旧两种切分粒度混在同一个集合里。
    """

    factors = (
        SIGNATURE_VERSION,
        settings.vectorstore_backend,
        settings.collection_name,
        f"chunk={settings.chunk_size}/{settings.chunk_overlap}",
        (
            "embedding="
            f"{settings.embedding.provider}:{settings.embedding.model}:"
            f"{settings.embedding.dimensions}"
        ),
    )
    return hashlib.sha256("|".join(factors).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class DocumentChange:
    """单个源文档的变更判定结果。"""

    source: str
    status: DocumentStatus
    #: 当前磁盘路径；``DELETED`` 时为 ``None``
    path: Path | None = None
    #: 当前内容摘要；``DELETED`` 时为空字符串
    content_hash: str = ""
    #: 清单中的既有记录；``NEW`` 时为 ``None``
    previous: DocumentRecord | None = None

    @property
    def previous_chunk_ids(self) -> tuple[str, ...]:
        """该文档在向量库中的旧片段 id（无旧记录时为空）。"""

        return self.previous.chunk_ids if self.previous is not None else ()


@dataclass(frozen=True, slots=True)
class ChangePlan:
    """一次增量索引的完整变更计划。"""

    changes: tuple[DocumentChange, ...] = ()
    #: 索引签名与清单不一致（切分 / Embedding / 集合配置变化）
    signature_changed: bool = False
    #: 判定阶段被跳过的文件及原因（如摘要计算失败）
    skipped: tuple[str, ...] = ()

    def select(self, status: DocumentStatus) -> tuple[DocumentChange, ...]:
        """返回指定状态的变更项。"""

        return tuple(change for change in self.changes if change.status is status)

    def count(self, status: DocumentStatus) -> int:
        """统计指定状态的文档数。"""

        return sum(1 for change in self.changes if change.status is status)

    def pending(self) -> tuple[DocumentChange, ...]:
        """需要写入向量的文档（NEW + CHANGED）。"""

        return tuple(change for change in self.changes if change.status in PENDING_STATUSES)

    def stale_chunk_ids(
        self,
        status: DocumentStatus,
        *,
        only_sources: Collection[str] | None = None,
    ) -> tuple[str, ...]:
        """返回该状态下需要从向量库清理的旧片段 id。

        Args:
            status: 目标状态（通常为 ``CHANGED`` 或 ``DELETED``）。
            only_sources: 只清理这些来源；``None`` 表示不限制。
                用于「源文件读取失败时保留旧向量」的保护。
        """

        ids: list[str] = []
        for change in self.select(status):
            if only_sources is not None and change.source not in only_sources:
                continue
            ids.extend(change.previous_chunk_ids)
        # 去重并保持顺序，避免同一 id 被重复提交删除
        return tuple(dict.fromkeys(ids))


def plan_changes(
    root: Path | str,
    manifest: IndexManifest,
    *,
    signature: str,
    rebuild: bool = False,
    force_reindex: bool = False,
) -> ChangePlan:
    """比对知识库目录与索引清单，产出变更计划。

    以下情况会让「内容未变」的文档也必须重新索引：

    - **索引签名不一致**（切分参数 / Embedding 模型 / 向量集合变了）：
      历史片段的粒度或向量语义已不可复用，因此判定为 ``CHANGED``，
      旧片段会被删除后重写；
    - ``force_reindex=True``（向量库被清空但清单仍在）：必须补齐内容，
      否则清单会一直误报 ``UNCHANGED`` 导致知识库永久为空；
    - ``rebuild=True``（全量重建）：集合已在调用前清空，
      因此现存文档一律视为 ``NEW`` 且不引用旧片段——它们的向量已经不存在，
      再去「删除旧片段」只是无意义的空操作。

    Args:
        root: 知识库根目录。
        manifest: 上一次的索引清单。
        signature: 当前索引签名。
        rebuild: 是否为全量重建（集合已清空）。
        force_reindex: 是否强制重新写入现存文档。

    Returns:
        变更计划；``DELETED`` 由「清单有、目录无」的差集得出，与上述两种模式无关。
    """

    knowledge_dir = Path(root)
    signature_changed = manifest.indexed_count > 0 and manifest.signature != signature
    reindex_all = force_reindex or signature_changed

    changes: list[DocumentChange] = []
    skipped: list[str] = []

    for path in iter_document_files(knowledge_dir):
        source = path.relative_to(knowledge_dir).as_posix()
        try:
            digest = hash_file(path)
        except OSError as exc:  # 权限 / 占用等：跳过而非中断整轮索引
            logger.warning("计算文件摘要失败，已跳过: %s (%s)", source, exc)
            skipped.append(f"{source}: {exc}")
            continue

        record = manifest.get(source)
        if rebuild or record is None:
            status = DocumentStatus.NEW
        elif record.content_hash != digest or reindex_all:
            status = DocumentStatus.CHANGED
        else:
            status = DocumentStatus.UNCHANGED

        changes.append(
            DocumentChange(
                source=source,
                status=status,
                path=path,
                content_hash=digest,
                # 全量重建时集合已清空，旧片段引用不再有意义
                previous=None if rebuild else record,
            )
        )

    present = {change.source for change in changes}
    for source, record in sorted(manifest.records.items()):
        if source not in present:
            changes.append(
                DocumentChange(source=source, status=DocumentStatus.DELETED, previous=record)
            )

    plan = ChangePlan(
        changes=tuple(changes),
        signature_changed=signature_changed,
        skipped=tuple(skipped),
    )
    logger.info(
        "变更计划: NEW=%d CHANGED=%d UNCHANGED=%d DELETED=%d（%s）",
        plan.count(DocumentStatus.NEW),
        plan.count(DocumentStatus.CHANGED),
        plan.count(DocumentStatus.UNCHANGED),
        plan.count(DocumentStatus.DELETED),
        "全量重建" if rebuild else ("强制重新索引" if reindex_all else "增量"),
    )
    return plan


__all__ = [
    "PENDING_STATUSES",
    "SIGNATURE_VERSION",
    "ChangePlan",
    "DocumentChange",
    "DocumentStatus",
    "compute_index_signature",
    "hash_file",
    "plan_changes",
]
