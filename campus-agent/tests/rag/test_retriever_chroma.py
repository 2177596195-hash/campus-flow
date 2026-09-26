"""KnowledgeRetriever 与真实 Chroma 的集成测试（离线）。

用确定性哈希向量驱动真实 Chroma 集合，不需要网络与真实 Embedding 服务。
验证的是「真实后端」这一侧的行为：分数是否落在 [0, 1]、是否越大越相关、
排序是否可靠、阈值与来源 metadata 是否按契约保留。
"""

from __future__ import annotations

import pytest
from langchain_chroma import Chroma

from app.rag.retriever import KnowledgeRetriever
from tests._fakes import HashingEmbeddings

#: (文本, metadata)，主题互不重叠，便于断言排序
DOCS: list[tuple[str, dict[str, object]]] = [
    (
        "图书馆的开放时间为周一至周五 8:00 至 22:00，周末 9:00 至 17:00。",
        {"source": "library.md", "title": "图书馆管理办法", "page": 1},
    ),
    (
        "学生宿舍禁止使用热得快、电饭锅等大功率电器。",
        {"source": "campus_rules.md", "title": "宿舍管理规定", "page": 3},
    ),
    (
        "奖学金评定依据综合素质测评成绩排名，按学年组织。",
        {"source": "scholarship.md", "title": "奖学金评定办法"},
    ),
]

QUERY = "图书馆开放时间"
COLLECTION = "retriever_test"


def _build_store(persist_dir: str, collection: str = COLLECTION) -> Chroma:
    """构建与生产配置一致的 Chroma 集合（余弦空间、持久化目录）。"""

    return Chroma(
        collection_name=collection,
        embedding_function=HashingEmbeddings(),
        persist_directory=persist_dir,
        collection_metadata={"hnsw:space": "cosine"},
    )


@pytest.fixture(scope="module")
def chroma_store(tmp_path_factory: pytest.TempPathFactory) -> Chroma:
    """已写入三条校园规章的真实 Chroma 集合。"""

    store = _build_store(str(tmp_path_factory.mktemp("chroma")))
    store.add_texts(
        texts=[text for text, _ in DOCS],
        metadatas=[dict(metadata) for _, metadata in DOCS],
    )
    return store


def test_real_chroma_scores_are_relevance_in_unit_range(chroma_store: Chroma) -> None:
    """真实后端返回的分数已统一为 [0, 1] 的相关性，而非原始距离。"""

    chunks = KnowledgeRetriever(chroma_store, top_k=3).retrieve(QUERY)

    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.score is not None
        assert 0.0 <= chunk.score <= 1.0


def test_real_chroma_ranks_most_relevant_first(chroma_store: Chroma) -> None:
    """最相关的片段（图书馆）排在首位，证明分数语义是「越大越相关」。"""

    assert KnowledgeRetriever(chroma_store, top_k=3).retrieve(QUERY)[0].source == "library.md"


def test_real_chroma_scores_are_descending(chroma_store: Chroma) -> None:
    """结果按分数降序返回。"""

    scores = [chunk.score for chunk in KnowledgeRetriever(chroma_store, top_k=3).retrieve(QUERY)]

    assert scores == sorted(scores, reverse=True)


def test_real_chroma_score_matches_store_own_conversion(chroma_store: Chroma) -> None:
    """检索器给出的分数与向量库自身的距离换算结果一致。"""

    raw = chroma_store.similarity_search_with_score(QUERY, k=3)
    to_relevance = chroma_store._select_relevance_score_fn()
    expected = sorted(
        (float(to_relevance(float(distance))) for _, distance in raw),
        reverse=True,
    )

    actual = [
        chunk.score for chunk in KnowledgeRetriever(chroma_store, top_k=3).retrieve(QUERY)
    ]

    assert actual == pytest.approx(expected)


def test_real_chroma_metadata_and_page_preserved(chroma_store: Chroma) -> None:
    """来源、标题、页码与 metadata 在真实后端上同样完整保留。"""

    chunk = next(
        item
        for item in KnowledgeRetriever(chroma_store, top_k=3).retrieve(QUERY)
        if item.source == "library.md"
    )

    assert chunk.page == 1
    assert chunk.title == "图书馆管理办法（第 1 页）"
    assert chunk.metadata["title"] == "图书馆管理办法"
    assert chunk.content in {text for text, _ in DOCS}


def test_real_chroma_threshold_applied_on_relevance_scale(chroma_store: Chroma) -> None:
    """阈值在真实分数上生效，且阈值语义为「含边界」。"""

    baseline = KnowledgeRetriever(chroma_store, top_k=3).retrieve(QUERY)
    assert len(baseline) >= 2

    cutoff = baseline[1].score
    assert cutoff is not None

    kept = KnowledgeRetriever(chroma_store, top_k=3, score_threshold=cutoff).retrieve(QUERY)

    assert len(kept) == len([chunk for chunk in baseline if chunk.score >= cutoff])
    assert all(chunk.score is not None and chunk.score >= cutoff for chunk in kept)


def test_real_chroma_empty_collection_returns_empty(tmp_path) -> None:
    """空集合返回空列表，不抛异常。"""

    store = _build_store(str(tmp_path / "empty"), collection="empty_collection")

    assert KnowledgeRetriever(store).retrieve(QUERY) == []


def test_real_chroma_retrieval_survives_store_reload(tmp_path) -> None:
    """重新加载已持久化的集合后检索依旧可用（对应重启进程的场景）。"""

    persist_dir = str(tmp_path / "reload")
    first = _build_store(persist_dir, collection="reload_collection")
    first.add_texts(
        texts=[text for text, _ in DOCS],
        metadatas=[dict(metadata) for _, metadata in DOCS],
    )

    reloaded = _build_store(persist_dir, collection="reload_collection")
    chunks = KnowledgeRetriever(reloaded, top_k=3).retrieve(QUERY)

    assert len(chunks) == 3
    assert chunks[0].source == "library.md"
