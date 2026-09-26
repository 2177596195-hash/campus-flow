"""KnowledgeRetriever 单元测试（完全离线）。

用只实现 LangChain 抽象的替身驱动，覆盖检索层对外承诺的每一项：
Top-K 召回、阈值过滤、分数降序、metadata 保留、空结果、无关查询、
以及「距离 -> 相关性」的换算与不可换算时的降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.documents import Document

from app.rag.retriever import retriever as retriever_module
from app.rag.retriever.retriever import KnowledgeRetriever
from app.rag.schema import RetrievedChunk
from tests._fakes import FakeVectorStore, cosine_relevance, make_document, make_rag_settings

# 距离乱序给出：检索器必须自行换算并排序，不能依赖后端返回顺序。
# 换算后相关性依次为 0.40（宿舍）/ 0.95（图书馆）/ 0.70（奖学金）。
LIBRARY_DISTANCE = 0.05
SCHOLARSHIP_DISTANCE = 0.30
DORM_DISTANCE = 0.60


@pytest.fixture
def hits() -> list[tuple[Document, float]]:
    """三条命中文档，顺序与相关性相反。"""

    return [
        (
            make_document(
                "宿舍内禁止使用大功率电器。", source="campus_rules.md", title="宿舍管理", page=3
            ),
            DORM_DISTANCE,
        ),
        (
            make_document(
                "图书馆开放时间为周一至周五 8:00-22:00。",
                source="library.md",
                title="图书馆",
                page=1,
            ),
            LIBRARY_DISTANCE,
        ),
        (
            make_document("奖学金评定按综合测评成绩排名。", source="scholarship.md", title="奖学金"),
            SCHOLARSHIP_DISTANCE,
        ),
    ]


@pytest.fixture
def store(hits: list[tuple[Document, float]]) -> FakeVectorStore:
    return FakeVectorStore(hits)


@pytest.fixture
def retriever(store: FakeVectorStore) -> KnowledgeRetriever:
    return KnowledgeRetriever(store, top_k=4)


# ------------------------------------------------------------------ 1. Top-K


def test_returns_top_k(store: FakeVectorStore) -> None:
    """正常查询按 top_k 截断，返回统一数据结构。"""

    chunks = KnowledgeRetriever(store, top_k=4).retrieve("图书馆开放时间")

    assert len(chunks) == 3
    assert all(isinstance(chunk, RetrievedChunk) for chunk in chunks)
    assert len(KnowledgeRetriever(store, top_k=2).retrieve("图书馆开放时间")) == 2


def test_top_k_can_be_overridden_per_call(store: FakeVectorStore) -> None:
    """``retrieve`` 的 top_k 参数覆盖构造时的默认值。"""

    retriever = KnowledgeRetriever(store, top_k=1)

    assert len(retriever.retrieve("图书馆开放时间")) == 1
    assert len(retriever.retrieve("图书馆开放时间", top_k=3)) == 3


def test_top_k_larger_than_hits_returns_all(store: FakeVectorStore) -> None:
    """召回数大于库内片段数时返回全部，不补空。"""

    assert len(KnowledgeRetriever(store, top_k=10).retrieve("图书馆开放时间")) == 3


# ------------------------------------------------------- 2. score_threshold


def test_score_threshold_filters_low_scores(store: FakeVectorStore) -> None:
    """阈值生效：低于阈值的片段被丢弃。"""

    retriever = KnowledgeRetriever(store, top_k=4, score_threshold=0.6)
    chunks = retriever.retrieve("图书馆开放时间")

    assert [chunk.source for chunk in chunks] == ["library.md", "scholarship.md"]
    assert all(chunk.score is not None and chunk.score >= 0.6 for chunk in chunks)


def test_score_threshold_none_keeps_everything(store: FakeVectorStore) -> None:
    """未配置阈值时不过滤。"""

    retriever = KnowledgeRetriever(store, top_k=4, score_threshold=None)

    assert len(retriever.retrieve("图书馆开放时间")) == 3
    assert retriever.score_threshold is None


def test_score_threshold_above_all_returns_empty(store: FakeVectorStore) -> None:
    """阈值高于全部分数时返回空列表而非报错。"""

    retriever = KnowledgeRetriever(store, top_k=4, score_threshold=0.99)

    assert retriever.retrieve("图书馆开放时间") == []


# ------------------------------------------------------------- 3. 排序语义


def test_results_sorted_by_score_desc(retriever: KnowledgeRetriever) -> None:
    """结果恒按分数从高到低排列，与后端返回顺序无关。"""

    scores = [chunk.score for chunk in retriever.retrieve("图书馆开放时间")]

    assert scores == sorted(scores, reverse=True)
    assert scores == pytest.approx([0.95, 0.70, 0.40])


def test_top_hit_is_most_relevant(retriever: KnowledgeRetriever) -> None:
    """距离最小的片段换算后相关性最高，排在首位。"""

    top = retriever.retrieve("图书馆开放时间")[0]

    assert top.source == "library.md"
    assert top.score == pytest.approx(0.95)


# --------------------------------------------------------- 4. metadata 保留


def test_metadata_and_page_preserved(retriever: KnowledgeRetriever) -> None:
    """content / source / title / page / metadata / score 全部保留。"""

    chunk = next(
        item for item in retriever.retrieve("图书馆开放时间") if item.source == "library.md"
    )

    assert chunk.content == "图书馆开放时间为周一至周五 8:00-22:00。"
    assert chunk.source == "library.md"
    assert chunk.score == pytest.approx(0.95)
    assert chunk.page == 1
    assert chunk.metadata["page"] == 1
    assert chunk.metadata["source"] == "library.md"
    assert chunk.title == "图书馆（第 1 页）"


def test_metadata_passthrough_of_extra_fields() -> None:
    """未识别的 metadata 字段原样透传，不做裁剪。"""

    document = make_document("片段内容", source="faq.txt", title="FAQ", chunk_index=7, topic="借阅")
    retriever = KnowledgeRetriever(FakeVectorStore([(document, 0.1)]))

    chunk = retriever.retrieve("借阅")[0]

    assert chunk.metadata["chunk_index"] == 7
    assert chunk.metadata["topic"] == "借阅"
    assert chunk.page is None


def test_missing_source_and_title_fall_back() -> None:
    """metadata 缺失时不抛异常，使用占位值。"""

    document = Document(page_content="无来源片段", metadata={})
    chunks = KnowledgeRetriever(FakeVectorStore([(document, 0.2)])).retrieve("查询")

    assert chunks[0].source == "未知来源"
    assert chunks[0].title == "未命名文档"


# ------------------------------------------------------------- 5. 空结果处理


def test_empty_store_returns_empty() -> None:
    """空向量库返回空列表。"""

    assert KnowledgeRetriever(FakeVectorStore([])).retrieve("图书馆开放时间") == []


def test_blank_query_short_circuits(store: FakeVectorStore) -> None:
    """空白查询直接返回空列表，不触达向量库。"""

    retriever = KnowledgeRetriever(store)

    assert retriever.retrieve("") == []
    assert retriever.retrieve("   ") == []
    assert store.score_search_calls == 0


def test_query_is_sanitized_before_search(store: FakeVectorStore) -> None:
    """查询串首尾空白与不可见字符（BOM / 零宽）在检索前被剔除。"""

    retriever = KnowledgeRetriever(store)

    retriever.retrieve("\ufeff\u200b图书馆开放时间 \n")

    assert store.last_query == "图书馆开放时间"


def test_invisible_only_query_short_circuits(store: FakeVectorStore) -> None:
    """只含不可见字符的查询等同于空查询。"""

    retriever = KnowledgeRetriever(store)

    assert retriever.retrieve("\ufeff\u200b") == []
    assert store.score_search_calls == 0


def test_store_failure_returns_empty_without_raising() -> None:
    """底层检索抛错时降级为空结果，不中断整轮对话。"""

    store = FakeVectorStore([(make_document("内容"), 0.1)], fail_score_search=True)

    assert KnowledgeRetriever(store).retrieve("查询") == []


# --------------------------------------------------------- 6. 不相关查询


def test_irrelevant_query_returns_low_scores_without_error() -> None:
    """库内无相关内容时返回低分片段，不抛异常。"""

    store = FakeVectorStore([(make_document("宿舍禁止使用大功率电器。"), 0.98)])

    chunks = KnowledgeRetriever(store).retrieve("今天股市行情如何")

    assert len(chunks) == 1
    assert chunks[0].score == pytest.approx(0.02)
    assert chunks[0].score >= 0.0


# ----------------------------------------------- 距离 -> 相关性 换算（核心）


def test_distance_converted_to_relevance_when_backend_lacks_it(
    hits: list[tuple[Document, float]],
) -> None:
    """后端只给距离时，检索器在本层换算成「越大越相关」。"""

    store = FakeVectorStore(hits, supports_relevance_scores=False)
    retriever = KnowledgeRetriever(store)

    scores = [chunk.score for chunk in retriever.retrieve("图书馆开放时间")]

    # 距离 0.60 / 0.05 / 0.30 -> 相关性 0.40 / 0.95 / 0.70，并重排为降序
    assert scores == pytest.approx([0.95, 0.70, 0.40])
    assert store.score_search_calls == 1


def test_threshold_uses_relevance_not_distance(
    hits: list[tuple[Document, float]],
) -> None:
    """阈值比较的是换算后的相关性，而不是原始距离。"""

    store = FakeVectorStore(hits, supports_relevance_scores=False)
    retriever = KnowledgeRetriever(store, score_threshold=0.6)

    # 若误把距离当分数，只有 0.60 那条会「过线」；正确实现应保留相关性 >= 0.6 的两条
    assert [chunk.source for chunk in retriever.retrieve("图书馆开放时间")] == [
        "library.md",
        "scholarship.md",
    ]


def test_explicit_relevance_fn_overrides_store(hits: list[tuple[Document, float]]) -> None:
    """显式传入换算函数时以其为准。"""

    store = FakeVectorStore(hits, relevance_fn=None, supports_relevance_scores=False)
    retriever = KnowledgeRetriever(store, relevance_score_fn=cosine_relevance)

    assert retriever.retrieve("图书馆开放时间")[0].score == pytest.approx(0.95)


def test_unknown_score_semantics_yields_none_score(
    hits: list[tuple[Document, float]],
) -> None:
    """无法换算时分数置 None，绝不把距离当成相关性。"""

    store = FakeVectorStore(hits, relevance_fn=None, supports_relevance_scores=False)

    chunks = KnowledgeRetriever(store).retrieve("图书馆开放时间")

    assert len(chunks) == 3
    assert all(chunk.score is None for chunk in chunks)


def test_none_scores_neither_filtered_nor_crash_sorting(
    hits: list[tuple[Document, float]],
) -> None:
    """分数未知时：不被阈值误杀，排序也不因 None 抛错（保持后端顺序）。"""

    store = FakeVectorStore(hits, relevance_fn=None, supports_relevance_scores=False)
    retriever = KnowledgeRetriever(store, score_threshold=0.9)

    chunks = retriever.retrieve("图书馆开放时间")

    assert [chunk.source for chunk in chunks] == [
        "campus_rules.md",
        "library.md",
        "scholarship.md",
    ]


# ------------------------------------------------------------ 配置与装配


def test_from_settings_reads_config() -> None:
    """``from_settings`` 把 top_k 与 score_threshold 从配置读入。"""

    store = FakeVectorStore(
        [
            (make_document("图书馆开放时间。", source="library.md", title="图书馆"), 0.05),
            (make_document("宿舍禁止大功率电器。", source="campus_rules.md", title="宿舍"), 0.60),
        ]
    )
    settings = make_rag_settings(top_k=1, score_threshold=0.5)

    retriever = KnowledgeRetriever.from_settings(store, settings)

    assert retriever.top_k == 1
    assert retriever.score_threshold == 0.5
    assert len(retriever.retrieve("图书馆开放时间")) == 1


def test_retriever_reports_configured_defaults(store: FakeVectorStore) -> None:
    """默认 top_k 与未配置阈值时的取值符合预期。"""

    retriever = KnowledgeRetriever(store)

    assert retriever.top_k == 4
    assert retriever.score_threshold is None


# --------------------------------------------------- 架构守卫（解耦要求）


def test_module_has_no_backend_specific_import() -> None:
    """检索层不得 import 任何具体向量库，也不得触碰后端私有属性。"""

    source = Path(retriever_module.__file__).read_text(encoding="utf-8")
    import_lines = [
        line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
    ]
    backends = ("chroma", "milvus", "faiss", "qdrant", "pgvector", "pinecone", "weaviate")

    for line in import_lines:
        for backend in backends:
            assert backend not in line.lower(), f"检索层出现后端专有依赖: {line.strip()}"

    assert "_collection" not in source
