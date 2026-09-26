"""增量索引（Index Manifest）测试：完全离线。

用真实 Chroma + 确定性哈希向量驱动，不联网、不需要真实 Embedding 服务，
因此既能验证「判定逻辑」，也能验证「向量库里到底剩下什么」。

覆盖：NEW / CHANGED / UNCHANGED / DELETED 的判定与执行、
CHANGED 后旧片段不残留、UNCHANGED 不重新 embedding、
清单字段与持久化、全量重建、索引签名变化、以及增量之后检索依旧可用。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_chroma import Chroma

from app.rag import (
    MANIFEST_FILENAME,
    DocumentStatus,
    IndexManifest,
    IngestReport,
    ManifestStore,
    compute_index_signature,
    hash_file,
    ingest_directory,
)
from app.rag.retriever import KnowledgeRetriever
from tests._fakes import CountingEmbeddings, make_ingest_settings

#: 初始知识库内容（虚拟校园规章，无真实个人信息）
DOCS: dict[str, str] = {
    "campus_rules.md": (
        "# 校园管理规定\n\n"
        "学生宿舍禁止使用热得快、电饭锅等大功率电器，违者按校纪处理。\n"
        "教学楼与图书馆内禁止吸烟，禁止携带食品进入阅览区。\n"
    ),
    "library.md": (
        "# 图书馆管理办法\n\n"
        "图书馆开放时间为周一至周五 8:00 至 22:00，周末 9:00 至 17:00。\n"
        "本科生每次可借阅图书 10 册，借期 30 天，可续借一次。\n"
    ),
    "scholarship.md": (
        "# 奖学金评定办法\n\n"
        "奖学金评定依据综合素质测评成绩排名，按学年组织，公示期不少于 5 个工作日。\n"
    ),
}

COLLECTION = "incremental_test"


class Harness:
    """一次增量索引测试所需的全部运行时对象。"""

    def __init__(self, root: Path, storage: Path) -> None:
        self.root = root
        self.storage = storage
        self.embeddings = CountingEmbeddings()
        self.settings = make_ingest_settings(
            knowledge_dir=root,
            persist_dir=storage,
            chunk_size=500,
            chunk_overlap=80,
        )
        self.manifest_store = ManifestStore(storage / MANIFEST_FILENAME)
        self.store = Chroma(
            collection_name=COLLECTION,
            embedding_function=self.embeddings,
            persist_directory=str(storage / "vectorstore"),
            collection_metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------------------ 文件操作

    def write(self, name: str, content: str) -> Path:
        """写入（或覆盖）一个知识库源文件。"""

        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def remove(self, name: str) -> None:
        """从知识库目录删除一个源文件。"""

        (self.root / name).unlink()

    # ------------------------------------------------------------------ 索引

    def ingest(self, *, full: bool = False) -> IngestReport:
        """执行一次索引（默认增量）。"""

        return ingest_directory(store=self.store, settings=self.settings, full=full)

    def bootstrap(self) -> IngestReport:
        """建立初始知识库：三份文档全部为 NEW。"""

        for name, content in DOCS.items():
            self.write(name, content)
        return self.ingest()

    # ------------------------------------------------------------------ 观测

    def ids(self) -> set[str]:
        """向量库中的全部片段 id。"""

        return set(self._payload()["ids"])

    def sources(self) -> Counter[str]:
        """向量库中每个来源的片段数。"""

        return Counter(str(m.get("source")) for m in self._payload()["metadatas"])

    def contents(self) -> list[str]:
        """向量库中的全部片段正文。"""

        return [str(text) for text in self.store.get(include=["documents"]).get("documents") or []]

    def manifest(self) -> IndexManifest:
        """从磁盘重新读取索引清单（不依赖进程内状态）。"""

        return self.manifest_store.load()

    def _payload(self) -> dict:
        payload = self.store.get(include=["metadatas"])
        payload.setdefault("ids", [])
        payload.setdefault("metadatas", [])
        return payload


def _manifest_ids(manifest: IndexManifest) -> set[str]:
    """清单中记录的全部片段 id。"""

    return {chunk_id for record in manifest.records.values() for chunk_id in record.chunk_ids}


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "knowledge", tmp_path / "storage")


# ---------------------------------------------------------------------- 状态判定


def test_first_ingest_marks_every_document_new(harness: Harness) -> None:
    """首次导入：全部识别为 NEW，并建立清单。"""

    report = harness.bootstrap()

    assert (report.new, report.changed, report.unchanged, report.deleted) == (3, 0, 0, 0)
    assert report.documents == 3
    assert set(harness.sources()) == set(DOCS)
    assert harness.manifest().indexed_count == 3
    assert harness.ids() == _manifest_ids(harness.manifest())


def test_second_ingest_is_all_unchanged(harness: Harness) -> None:
    """内容未变：全部 UNCHANGED，且向量库内容逐位不变。"""

    harness.bootstrap()
    ids_before = harness.ids()

    report = harness.ingest()

    assert (report.new, report.changed, report.unchanged, report.deleted) == (0, 0, 3, 0)
    assert report.documents == 0
    assert report.chunks == 0
    assert harness.ids() == ids_before


def test_unchanged_documents_are_not_reembedded(harness: Harness) -> None:
    """UNCHANGED 不应触发任何 Embedding 调用。"""

    harness.bootstrap()
    harness.embeddings.reset_counters()

    harness.ingest()

    assert harness.embeddings.document_calls == 0
    assert harness.embeddings.document_texts == 0


def test_unchanged_document_keeps_its_indexed_at(harness: Harness) -> None:
    """UNCHANGED 不重写记录：``indexed_at`` 保持不变。"""

    harness.bootstrap()
    before = harness.manifest().get("library.md")

    harness.ingest()

    after = harness.manifest().get("library.md")
    assert before is not None and after is not None
    assert after.indexed_at == before.indexed_at


def test_modified_document_is_the_only_change(harness: Harness) -> None:
    """修改一个文档：只有它 CHANGED，其余保持 UNCHANGED。"""

    harness.bootstrap()
    before = harness.manifest().get("library.md")
    harness.embeddings.reset_counters()

    harness.write("library.md", DOCS["library.md"] + "寒暑假期间开放时间另行通知。\n")
    report = harness.ingest()

    assert (report.new, report.changed, report.unchanged, report.deleted) == (0, 1, 2, 0)
    after = harness.manifest().get("library.md")
    assert before is not None and after is not None
    assert after.content_hash != before.content_hash
    assert harness.embeddings.document_calls == 1


def test_added_document_is_the_only_new_one(harness: Harness) -> None:
    """新增一个文档：只有它 NEW。"""

    harness.bootstrap()
    harness.write("sports.md", "# 体育场馆管理办法\n\n体育馆与羽毛球馆需提前一天通过校园卡预约。\n")

    report = harness.ingest()

    assert (report.new, report.changed, report.unchanged, report.deleted) == (1, 0, 3, 0)
    assert "sports.md" in harness.sources()
    assert harness.manifest().get("sports.md") is not None


def test_deleted_document_is_recognized(harness: Harness) -> None:
    """删除一个文档：识别为 DELETED，片段从向量库清除、记录从清单移除。"""

    harness.bootstrap()
    record = harness.manifest().get("scholarship.md")
    assert record is not None and record.chunk_ids
    harness.remove("scholarship.md")

    report = harness.ingest()

    assert (report.new, report.changed, report.unchanged, report.deleted) == (0, 0, 2, 1)
    assert report.removed_chunks == len(record.chunk_ids)
    assert "scholarship.md" not in harness.sources()
    assert harness.ids().isdisjoint(set(record.chunk_ids))
    assert harness.manifest().get("scholarship.md") is None


def test_empty_knowledge_dir_deletes_everything(harness: Harness) -> None:
    """知识目录被清空：全部 DELETED，向量库为空。"""

    harness.bootstrap()
    for name in DOCS:
        harness.remove(name)

    report = harness.ingest()

    assert report.deleted == 3
    assert harness.sources() == Counter()
    assert harness.manifest().indexed_count == 0
    assert harness.manifest().total_chunks() == 0


# ---------------------------------------------------------------------- 片段清理


def test_changed_document_leaves_no_stale_chunks(harness: Harness) -> None:
    """CHANGED 必须「先删后写」：旧片段 id 与旧正文都不再存在。"""

    harness.bootstrap()
    old_record = harness.manifest().get("library.md")
    assert old_record is not None
    stale_candidates = set(old_record.chunk_ids)

    harness.write(
        "library.md",
        "# 图书馆管理办法\n\n图书馆开放时间调整为 7:30 至 23:00，借阅册数不变。\n",
    )
    harness.ingest()

    new_record = harness.manifest().get("library.md")
    assert new_record is not None
    stale = stale_candidates - set(new_record.chunk_ids)
    assert stale, "内容变化后应产生新的片段 id"

    assert harness.ids().isdisjoint(stale), "旧片段必须已被删除"
    assert harness.ids() == _manifest_ids(harness.manifest())
    assert all("借期 30 天" not in text for text in harness.contents())


def test_store_content_matches_manifest_after_mixed_changes(harness: Harness) -> None:
    """增删改混合后，向量库内容与清单记录完全一致（无残留、无遗漏）。"""

    harness.bootstrap()

    harness.write("library.md", DOCS["library.md"] + "新增：电子阅览室 24 小时开放。\n")
    harness.write("sports.md", "# 体育场馆管理办法\n\n体育馆需提前一天预约。\n")
    harness.remove("campus_rules.md")
    harness.ingest()

    assert harness.ids() == _manifest_ids(harness.manifest())
    assert set(harness.sources()) == {"library.md", "scholarship.md", "sports.md"}


# ---------------------------------------------------------------------- 清单


def test_manifest_records_required_fields(harness: Harness) -> None:
    """清单记录包含 source / content_hash / indexed_at / chunk_count。"""

    harness.bootstrap()
    record = harness.manifest().get("campus_rules.md")

    assert record is not None
    assert record.source == "campus_rules.md"
    assert record.content_hash == hash_file(harness.root / "campus_rules.md")
    assert record.indexed_at.endswith("+00:00")
    assert record.chunk_count == harness.sources()["campus_rules.md"]
    assert len(record.chunk_ids) == record.chunk_count


def test_manifest_file_is_json_under_persist_dir(harness: Harness) -> None:
    """清单落盘为 persist_dir 下的可读 JSON。"""

    harness.bootstrap()

    path = harness.storage / MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert payload["signature"] == compute_index_signature(harness.settings)
    assert [item["source"] for item in payload["documents"]] == sorted(DOCS)


def test_manifest_survives_reload(harness: Harness) -> None:
    """重新加载清单（模拟进程重启）后仍能识别 UNCHANGED。"""

    harness.bootstrap()

    reloaded = ManifestStore(harness.storage / MANIFEST_FILENAME).load()

    assert reloaded.indexed_count == 3
    assert reloaded.known_chunk_ids() == harness.ids()


def test_corrupt_manifest_falls_back_to_full_reindex(harness: Harness) -> None:
    """清单损坏时按空清单处理：全部 NEW，重建清单而不是漏索引。"""

    harness.bootstrap()
    (harness.storage / MANIFEST_FILENAME).write_text("{ 不是合法 JSON", encoding="utf-8")

    report = harness.ingest()

    assert (report.new, report.unchanged) == (3, 0)
    assert harness.ids() == _manifest_ids(harness.manifest())


def test_missing_vectors_with_intact_manifest_forces_reindex(harness: Harness) -> None:
    """向量库被清空而清单仍在时必须重新写入，否则知识库会永久为空。"""

    harness.bootstrap()
    harness.store.reset_collection()
    assert harness.ids() == set()

    report = harness.ingest()

    assert report.changed == 3
    assert harness.ids() == _manifest_ids(harness.manifest())


# ---------------------------------------------------------------------- 全量与签名


def test_full_reindex_rebuilds_collection(harness: Harness) -> None:
    """``full=True``：清空集合后全部重写，并重新向量化。"""

    harness.bootstrap()
    harness.embeddings.reset_counters()

    report = harness.ingest(full=True)

    assert report.rebuilt is True
    assert (report.new, report.changed, report.unchanged, report.deleted) == (3, 0, 0, 0)
    assert harness.embeddings.document_calls >= 1
    assert harness.sources() == Counter(
        {
            "campus_rules.md": harness.manifest().get("campus_rules.md").chunk_count,
            "library.md": harness.manifest().get("library.md").chunk_count,
            "scholarship.md": harness.manifest().get("scholarship.md").chunk_count,
        }
    )


def test_full_reindex_drops_orphan_chunks(harness: Harness) -> None:
    """全量重建会清掉库里不受清单管理的遗留片段。"""

    harness.bootstrap()
    harness.store.add_texts(
        texts=["历史遗留的过期片段"],
        metadatas=[{"source": "legacy.md", "title": "旧数据"}],
    )
    assert "legacy.md" in harness.sources()

    harness.ingest(full=True)

    assert "legacy.md" not in harness.sources()
    assert harness.ids() == _manifest_ids(harness.manifest())


def test_index_signature_change_triggers_reindex(harness: Harness) -> None:
    """切分参数变化（索引签名不一致）时，已有文档全部重新索引。"""

    harness.bootstrap()
    before = harness.manifest()

    changed_settings = replace(harness.settings, chunk_size=200, chunk_overlap=20)
    report = ingest_directory(store=harness.store, settings=changed_settings)

    assert (report.new, report.changed, report.unchanged) == (0, 3, 0)
    assert harness.manifest().signature == compute_index_signature(changed_settings)
    assert harness.ids() == _manifest_ids(harness.manifest())
    # 旧切分粒度下的片段已被清理
    assert harness.ids().isdisjoint(before.known_chunk_ids() - _manifest_ids(harness.manifest()))


# ---------------------------------------------------------------------- 检索回归


def test_retrieval_works_after_incremental_ingest(harness: Harness) -> None:
    """增量索引后检索依旧可用：新文档可召回，已删除文档不再出现。"""

    harness.bootstrap()
    harness.write(
        "sports.md",
        "# 体育场馆管理办法\n\n体育馆与羽毛球馆需提前一天通过校园卡预约，周末开放至 21:00。\n",
    )
    harness.remove("scholarship.md")
    harness.ingest()

    retriever = KnowledgeRetriever(harness.store, top_k=4)

    hits = retriever.retrieve("体育馆怎么预约")
    assert hits and hits[0].source == "sports.md"

    assert all(chunk.source != "scholarship.md" for chunk in retriever.retrieve("奖学金评定依据"))


def test_report_format_is_stable(harness: Harness) -> None:
    """报告前五行是固定的运维解析约定。"""

    harness.bootstrap()
    report = harness.ingest()

    assert report.format_report().splitlines()[:5] == [
        "NEW: 0",
        "CHANGED: 0",
        "UNCHANGED: 3",
        "DELETED: 0",
        f"TOTAL_CHUNKS: {report.total_chunks}",
    ]


def test_document_status_values_match_spec() -> None:
    """四种状态名与约定一致。"""

    assert {status.value for status in DocumentStatus} == {
        "NEW",
        "CHANGED",
        "UNCHANGED",
        "DELETED",
    }
