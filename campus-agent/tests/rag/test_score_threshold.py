"""RAG_SCORE_THRESHOLD 策略测试（完全离线）。

本文件只聚焦「相关性阈值」这一条检索策略本身，不重复
``test_retriever.py`` 已覆盖的距离换算、降序排序、后端解耦等内容。

覆盖点：

1. 未配置（``None``）时与不传阈值完全等价（保持旧行为）；
2. ``0`` 保留全部合法分数、``1`` 只保留满分；
3. **边界值 ``score == threshold`` 必须保留**（判定是 ``>=`` 而非 ``>``）；
4. 严格小于阈值被过滤，过滤后仍保持降序；
5. 无候选 / 全部被过滤时返回空列表而不抛异常；
6. 分数未知（``None``）时保守放行，不被阈值误杀；
7. ``top_k`` 是「先截断、后过滤」的召回窗口，阈值只做拒绝、不做补位；
8. 阈值可经 ``from_settings`` 从配置流入；
9. **项目当前正式阈值 ``0.4``** 的边界回归：恰好 0.4 保留、0.3984375 被过滤、
   且默认配置里不存在写死的业务阈值。

替身向量库返回的是**距离**，候选顺序刻意打乱：距离取 0 / 0.125 / 0.25 / 0.5 / 1.0，
换算后相关性精确落在 1.0 / 0.875 / 0.75 / 0.5 / 0.0（均为二进制精确值），
因此边界断言可以直接用 ``==``，不受浮点误差干扰。
"""

from __future__ import annotations

from langchain_core.documents import Document

from app.rag.retriever import KnowledgeRetriever
from tests._fakes import FakeVectorStore, make_document, make_rag_settings

QUERY = "开放时间"

# 距离 -> 相关性（1 - 距离），全部为二进制精确值
PERFECT_DISTANCE = 0.0  # -> 1.000
NEAR_DISTANCE = 0.125  # -> 0.875
MID_DISTANCE = 0.25  # -> 0.750
LOW_DISTANCE = 0.5  # -> 0.500
ZERO_DISTANCE = 1.0  # -> 0.000

#: 未过滤时的期望顺序（相关性降序）
RANKED_SOURCES = [
    "library.md",
    "campus_sports.pdf",
    "scholarship.md",
    "campus_faq.txt",
    "campus_rules.md",
]


def _hits() -> list[tuple[Document, float]]:
    """五条候选，存储顺序与相关性无关。"""

    return [
        (make_document("校园网信息费每学期 60 元。", source="campus_faq.txt"), LOW_DISTANCE),
        (make_document("图书馆开放至 22:00。", source="library.md"), PERFECT_DISTANCE),
        (make_document("宿舍禁止使用大功率电器。", source="campus_rules.md"), ZERO_DISTANCE),
        (make_document("奖学金按综合测评排名。", source="scholarship.md"), MID_DISTANCE),
        (make_document("体育馆开放至 22:00。", source="campus_sports.pdf"), NEAR_DISTANCE),
    ]


def _retriever(
    *,
    top_k: int = 5,
    score_threshold: float | None = None,
    hits: list[tuple[Document, float]] | None = None,
) -> KnowledgeRetriever:
    return KnowledgeRetriever(
        FakeVectorStore(hits if hits is not None else _hits()),
        top_k=top_k,
        score_threshold=score_threshold,
    )


def _sources(retriever: KnowledgeRetriever) -> list[str]:
    return [chunk.source for chunk in retriever.retrieve(QUERY)]


# ------------------------------------------------- 1. 未配置阈值：保持旧行为


def test_threshold_none_matches_default_behaviour() -> None:
    """``score_threshold=None`` 与不传该参数完全等价（不改变现有默认行为）。"""

    baseline = KnowledgeRetriever(FakeVectorStore(_hits()), top_k=5).retrieve(QUERY)
    configured = _retriever(score_threshold=None).retrieve(QUERY)

    assert [(c.source, c.score) for c in configured] == [(c.source, c.score) for c in baseline]
    assert [c.source for c in configured] == RANKED_SOURCES


def test_threshold_none_reports_unset() -> None:
    """未配置时 ``score_threshold`` 属性为 ``None``（配置默认继续为空）。"""

    assert _retriever(score_threshold=None).score_threshold is None


# ------------------------------------------------------- 2. 两端极值 0 与 1


def test_threshold_zero_keeps_all_valid_scores() -> None:
    """阈值为 0 时保留全部合法分数，含恰好为 0 的那条。"""

    chunks = _retriever(score_threshold=0.0).retrieve(QUERY)

    assert [c.source for c in chunks] == RANKED_SOURCES
    assert chunks[-1].score == 0.0


def test_threshold_one_keeps_only_perfect_score() -> None:
    """阈值为 1 时只保留满分的候选。"""

    chunks = _retriever(score_threshold=1.0).retrieve(QUERY)

    assert [c.source for c in chunks] == ["library.md"]
    assert chunks[0].score == 1.0


# --------------------------------------------------------- 3. 边界与过滤方向


def test_score_equal_to_threshold_is_kept() -> None:
    """边界：分数恰好等于阈值时必须保留（判定用 ``>=``）。"""

    chunks = _retriever(score_threshold=0.75).retrieve(QUERY)

    assert [c.source for c in chunks] == ["library.md", "campus_sports.pdf", "scholarship.md"]
    assert chunks[-1].score == 0.75  # 恰好等于阈值，仍在结果内


def test_score_below_threshold_is_filtered() -> None:
    """严格小于阈值的候选被过滤。"""

    chunks = _retriever(score_threshold=0.875).retrieve(QUERY)

    # 0.875 的候选保留（等于阈值），0.75 的候选被丢弃（小于阈值）
    assert [c.source for c in chunks] == ["library.md", "campus_sports.pdf"]
    assert all(chunk.score is not None and chunk.score >= 0.875 for chunk in chunks)


def test_filtering_preserves_descending_order() -> None:
    """过滤只做裁剪，不改变「越大越相关」的降序约定。"""

    chunks = _retriever(score_threshold=0.5).retrieve(QUERY)
    scores = [chunk.score for chunk in chunks]

    assert scores == sorted(scores, reverse=True)
    assert scores == [1.0, 0.875, 0.75, 0.5]


# ------------------------------------------------------------- 4. 空结果处理


def test_empty_store_with_threshold_returns_empty() -> None:
    """向量库为空时配合阈值仍正常返回空列表。"""

    assert _retriever(score_threshold=0.4, hits=[]).retrieve(QUERY) == []


def test_all_filtered_returns_empty_without_raising() -> None:
    """全部候选都低于阈值时返回空列表，而不是抛异常。"""

    low_only = [(make_document("低相关片段。", source="weak.md"), 0.5)]  # 相关性 0.5

    assert _retriever(score_threshold=1.0, hits=low_only).retrieve(QUERY) == []


def test_unknown_scores_are_not_filtered_by_threshold() -> None:
    """分数语义未知（``None``）时保守放行：无法判定就不丢弃。

    这是刻意的策略——宁可把「无分数」的候选交给上层，也不因为无法比较
    而静默吞掉召回结果；阈值只对可比较的合法分数生效。
    """

    store = FakeVectorStore(_hits(), relevance_fn=None, supports_relevance_scores=False)
    chunks = KnowledgeRetriever(store, top_k=5, score_threshold=1.0).retrieve(QUERY)

    assert len(chunks) == 5
    assert all(chunk.score is None for chunk in chunks)


# ------------------------------------------------- 5. top_k 与阈值的先后关系


def test_threshold_yields_at_most_top_k_and_never_backfills() -> None:
    """``top_k`` 是先截断的召回窗口，阈值在其后过滤且不补位。

    因此过线条数可能**少于** ``top_k``：窗口内被阈值拒绝的候选，
    不会被窗口外的低分候选顶替。想要更多过线结果只能提高 ``top_k``。
    """

    narrow = _retriever(top_k=2, score_threshold=0.6).retrieve(QUERY)
    wide = _retriever(top_k=5, score_threshold=0.6).retrieve(QUERY)

    # top_k=2 的窗口里是「校园网（0.5）+ 图书馆（1.0）」，过滤后只剩 1 条
    assert [c.source for c in narrow] == ["library.md"]
    # 放大窗口后，同样的阈值多留下两条过线候选
    assert [c.source for c in wide] == ["library.md", "campus_sports.pdf", "scholarship.md"]
    assert len(narrow) < 2


def test_threshold_never_exceeds_top_k() -> None:
    """阈值不过滤时条数仍受 ``top_k`` 约束，阈值不会让结果变多。"""

    without_threshold = _retriever(top_k=3, score_threshold=None).retrieve(QUERY)
    with_low_threshold = _retriever(top_k=3, score_threshold=0.0).retrieve(QUERY)

    assert len(without_threshold) == len(with_low_threshold) == 3


# --------------------------------------------------------- 6. 从配置流入阈值


def test_threshold_flows_from_settings() -> None:
    """``from_settings`` 把配置里的阈值与 top_k 一并装配进检索器。"""

    settings = make_rag_settings(top_k=5, score_threshold=0.75)
    retriever = KnowledgeRetriever.from_settings(FakeVectorStore(_hits()), settings)

    assert retriever.score_threshold == 0.75
    assert retriever.top_k == 5
    assert _sources(retriever) == ["library.md", "campus_sports.pdf", "scholarship.md"]


# ============================== 7. 项目当前正式阈值 0.4（回归保护）

#: 项目当前正式启用的阈值（与 ``.env`` / ``.env.example`` 一致，详见 tests/test_config.py）。
CURRENT_THRESHOLD = 0.4

# 候选距离刻意取成「换算后」的精确值，使边界断言可用 ``==``：
#   1.0 - 0.4       == 0.4        恰好等于阈值 -> **必须保留**
#   1.0 - 0.6015625 == 0.3984375  严格小于阈值 -> **必须过滤**
BOUNDARY_DISTANCE = 1.0 - CURRENT_THRESHOLD  # -> 0.4
JUST_BELOW_DISTANCE = 0.6015625  # -> 0.3984375
JUST_BELOW_RELEVANCE = 1.0 - JUST_BELOW_DISTANCE


def _threshold_hits() -> list[tuple[Document, float]]:
    """围绕 0.4 的候选：满分 / 高于 / **恰好等于** / 略低 / 远低。"""

    return [
        (make_document("远低相关片段。", source="weak.md"), 0.75),  # -> 0.25
        (make_document("恰好等于阈值的片段。", source="boundary.md"), BOUNDARY_DISTANCE),  # -> 0.4
        (make_document("图书馆开放至 22:00。", source="library.md"), PERFECT_DISTANCE),  # -> 1.0
        (make_document("略低于阈值的片段。", source="just_below.md"), JUST_BELOW_DISTANCE),  # -> 0.3984375
        (make_document("高于阈值的片段。", source="above.md"), LOW_DISTANCE),  # -> 0.5
    ]


def test_current_threshold_keeps_a_score_equal_to_four_tenths() -> None:
    """边界：相关性恰好等于 0.4 的候选必须保留（判定是 ``>=`` 不是 ``>``）。"""

    chunks = _retriever(score_threshold=CURRENT_THRESHOLD, hits=_threshold_hits()).retrieve(QUERY)

    assert [c.source for c in chunks] == ["library.md", "above.md", "boundary.md"]
    assert chunks[-1].score == CURRENT_THRESHOLD


def test_current_threshold_filters_a_score_just_below_four_tenths() -> None:
    """严格小于 0.4 的候选（0.3984375）必须被过滤，且不补位。"""

    chunks = _retriever(score_threshold=CURRENT_THRESHOLD, hits=_threshold_hits()).retrieve(QUERY)

    assert [c.source for c in chunks] == ["library.md", "above.md", "boundary.md"]
    assert JUST_BELOW_RELEVANCE < CURRENT_THRESHOLD  # 前提：确实略低于阈值
    assert all(c.score is not None and c.score >= CURRENT_THRESHOLD for c in chunks)


def test_current_threshold_flows_from_settings_into_retriever() -> None:
    """配置层的 0.4 经 ``from_settings`` 原样装配进检索器（不重写、不取整）。"""

    settings = make_rag_settings(top_k=5, score_threshold=CURRENT_THRESHOLD)
    retriever = KnowledgeRetriever.from_settings(FakeVectorStore(_threshold_hits()), settings)

    assert retriever.score_threshold == CURRENT_THRESHOLD
    assert _sources(retriever) == ["library.md", "above.md", "boundary.md"]


def test_default_settings_do_not_hardcode_the_business_threshold() -> None:
    """默认配置里**不存在**写死的业务阈值：0.4 只来自配置，不来自代码。"""

    assert make_rag_settings().score_threshold is None
    assert _retriever(score_threshold=None).retrieve(QUERY)  # 未配置 = 不额外过滤
