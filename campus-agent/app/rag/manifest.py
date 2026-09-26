"""索引清单（Index Manifest）：记录「哪些源文档已被索引、索引成了哪些片段」。

职责边界：只管索引状态的持久化，不做加载、切分、向量化与检索。
之所以独立成模块：增量索引的正确性完全依赖这份状态，
把它与向量库、检索器解耦后，任何一方被替换都不影响历史索引记录。

存储形态：单个 JSON 文件（默认 ``<RAG_PERSIST_DIR>/index_manifest.json``）。
选 JSON 而非 SQLite 的理由是当前知识库规模很小（数十份文档），
单文件足够且可读、可手工检查、不引入额外依赖；若将来需要多实例并发写入，
只需替换 ``ManifestStore`` 的实现，``incremental`` 与 ``pipeline`` 的调用方式不变。

为什么记录 ``chunk_ids``：删除/更新时需要精确定位「某个 source 的全部片段」。
按 id 删除走的是 ``VectorStore.delete`` 抽象，与后端无关；
而按 metadata 过滤（``get(where=...)``）属于 Chroma 专有 API，
Milvus 等后端语义不同，会把向量库实现细节泄露到上层。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from app.config.settings import RAGSettings

logger = logging.getLogger(__name__)

#: 清单文件名；默认与向量库同目录
MANIFEST_FILENAME: Final[str] = "index_manifest.json"
#: 清单结构版本：结构发生不兼容变更时递增，旧文件将被视为空清单（触发全量重索引）
MANIFEST_VERSION: Final[int] = 1


def utc_now() -> str:
    """当前 UTC 时间（秒级 ISO-8601），用于 ``indexed_at`` / ``updated_at``。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_manifest_path(settings: RAGSettings) -> Path:
    """返回清单文件路径。

    未显式配置 ``RAG_MANIFEST_PATH`` 时落在向量库持久化目录内：
    这样「清单」与「向量」同生共死，删除 ``.storage`` 即等于同时清空两者，
    不会出现一方残留导致状态错位。
    """

    return settings.manifest_path or (settings.persist_dir / MANIFEST_FILENAME)


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """单个源文档的索引记录。"""

    #: 源文档唯一标识：相对知识库根目录的 posix 路径，
    #: 与向量库中 ``metadata["source"]`` 完全一致
    source: str
    #: 源文件字节内容的 sha256，用于判定 NEW / CHANGED / UNCHANGED
    content_hash: str
    #: 最近一次完成索引的时间（UTC ISO-8601）
    indexed_at: str
    #: 该源文档当前在向量库中的片段数
    chunk_count: int
    #: 该源文档写入向量库的全部片段 id，删除/更新时据此精确定位
    chunk_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入清单文件的结构。"""

        return {
            "source": self.source,
            "content_hash": self.content_hash,
            "indexed_at": self.indexed_at,
            "chunk_count": self.chunk_count,
            "chunk_ids": list(self.chunk_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DocumentRecord | None:
        """从清单结构还原记录；字段缺失或非法时返回 ``None``（按未索引处理）。

        单条记录损坏不应当破坏整份清单：最坏结果是该文档被当成 NEW 重新索引，
        ``chunk_id`` 由内容派生，重复写入不会产生重复向量。
        """

        try:
            source = str(payload["source"])
            content_hash = str(payload["content_hash"])
        except (KeyError, TypeError):
            logger.warning("清单记录缺少必要字段，已忽略: %s", payload)
            return None

        if not source or not content_hash:
            logger.warning("清单记录的 source/content_hash 为空，已忽略: %s", payload)
            return None

        try:
            chunk_count = int(payload.get("chunk_count") or 0)
        except (TypeError, ValueError):
            chunk_count = 0

        raw_ids = payload.get("chunk_ids") or ()
        chunk_ids = (
            tuple(str(item) for item in raw_ids)
            if isinstance(raw_ids, (list, tuple))
            else ()
        )

        return cls(
            source=source,
            content_hash=content_hash,
            indexed_at=str(payload.get("indexed_at") or ""),
            chunk_count=chunk_count,
            chunk_ids=chunk_ids,
        )


@dataclass(frozen=True, slots=True)
class IndexManifest:
    """索引清单：某一时刻的索引状态快照。

    不可变，任何更新都通过 ``evolve`` 返回新实例，
    避免调用方原地修改后忘记写回文件。
    """

    #: 索引签名：切分 / Embedding / 集合等影响向量语义的配置指纹。
    #: 与当前配置不一致时，历史片段虽然还在库里，但已不可直接复用。
    signature: str = ""
    #: 最近一次更新时间
    updated_at: str = ""
    #: source -> 记录
    records: Mapping[str, DocumentRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 映射也设为只读，防止绕过 evolve 原地修改
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))

    def get(self, source: str) -> DocumentRecord | None:
        """按来源取得记录；未索引过时返回 ``None``。"""

        return self.records.get(source)

    def sources(self) -> tuple[str, ...]:
        """返回全部已索引来源（有序，便于比对与展示）。"""

        return tuple(sorted(self.records))

    @property
    def indexed_count(self) -> int:
        """已索引的文档数。"""

        return len(self.records)

    def total_chunks(self) -> int:
        """清单记录的片段总数（可用于与向量库实际计数交叉校验）。"""

        return sum(record.chunk_count for record in self.records.values())

    def known_chunk_ids(self) -> frozenset[str]:
        """返回清单记录的片段 id 全集。"""

        return frozenset(
            chunk_id for record in self.records.values() for chunk_id in record.chunk_ids
        )

    def evolve(
        self,
        *,
        upserts: Iterable[DocumentRecord] = (),
        removed: Iterable[str] = (),
        signature: str | None = None,
        updated_at: str | None = None,
    ) -> IndexManifest:
        """在现有记录之上增删，返回新清单实例。

        Args:
            upserts: 需要新增或覆盖的记录。
            removed: 需要删除的来源。
            signature: 新的索引签名；``None`` 表示保持不变。
            updated_at: 新的更新时间；``None`` 表示保持不变。
        """

        merged = dict(self.records)
        for record in upserts:
            merged[record.source] = record
        for source in removed:
            merged.pop(source, None)

        return IndexManifest(
            signature=self.signature if signature is None else signature,
            updated_at=updated_at or self.updated_at,
            records=merged,
        )


class ManifestStore:
    """索引清单的读写（JSON 文件，原子写入）。

    写入使用「临时文件 + 原子替换」：进程在写一半时中断也不会留下半份清单。
    读取时对缺失 / 损坏 / 版本不匹配的文件一律按空清单处理——
    最坏结果是全量重新索引，而不会误判为 UNCHANGED 导致漏索引。
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """清单文件路径。"""

        return self._path

    def load(self) -> IndexManifest:
        """读取清单；文件不存在或不可用时返回空清单。"""

        if not self._path.is_file():
            logger.debug("索引清单不存在，按空清单处理: %s", self._path)
            return IndexManifest()

        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("索引清单不可读，将重新索引全部文档: %s", self._path, exc_info=True)
            return IndexManifest()

        if not isinstance(payload, dict):
            logger.warning("索引清单结构非法，按空清单处理: %s", self._path)
            return IndexManifest()

        version = payload.get("version")
        if version != MANIFEST_VERSION:
            logger.warning(
                "索引清单版本不匹配（文件 v%s，程序 v%d），按空清单重新索引: %s",
                version,
                MANIFEST_VERSION,
                self._path,
            )
            return IndexManifest()

        records: dict[str, DocumentRecord] = {}
        for item in payload.get("documents") or ():
            if not isinstance(item, dict):
                logger.warning("清单文档项结构非法，已忽略: %r", item)
                continue
            record = DocumentRecord.from_dict(item)
            if record is not None:
                records[record.source] = record

        manifest = IndexManifest(
            signature=str(payload.get("signature") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            records=records,
        )
        logger.debug("索引清单已加载: %s（文档 %d 份）", self._path, manifest.indexed_count)
        return manifest

    def save(self, manifest: IndexManifest) -> None:
        """原子写入清单。

        Raises:
            OSError: 磁盘不可写等持久化失败；此时向量已写入但状态未记录，
                下一次索引会把这些文档重新处理一遍（片段 id 由内容派生，不会重复）。
        """

        payload = {
            "version": MANIFEST_VERSION,
            "signature": manifest.signature,
            "updated_at": manifest.updated_at or utc_now(),
            "documents": [
                record.to_dict()
                for record in sorted(manifest.records.values(), key=lambda item: item.source)
            ],
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._path)
        except OSError:
            logger.exception("索引清单写入失败: %s", self._path)
            raise

        logger.debug("索引清单已更新: %s（文档 %d 份）", self._path, manifest.indexed_count)


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "DocumentRecord",
    "IndexManifest",
    "ManifestStore",
    "resolve_manifest_path",
    "utc_now",
]
