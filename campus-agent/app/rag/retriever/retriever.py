"""检索器封装。

职责边界（只做一件事）：把「一次查询 -> 若干带来源的相关片段」收口在此。
上层（RAG Service / knowledge 节点）不持有 ``VectorStore``，
因此换向量库、加混合检索、加重排序都只影响本模块。

对外三项保证：

1. **不感知具体后端**：只依赖 ``langchain_core`` 的 ``VectorStore`` 抽象，
   模块内不出现任何向量库专有类型；
2. **分数语义统一**：对外恒为「越大越相关」（LangChain 的相关性分数约定，
   取值 ``[0, 1]``）。底层若返回距离，在本层完成换算，
   因此 ``score_threshold`` 的含义与具体后端无关：Chroma 与 Milvus 配置一致；
3. **结果恒有序**：返回列表按分数降序排列，调用方无需再排序。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import VectorStore

from app.config.settings import RAGSettings, get_rag_settings
from app.rag.schema import RetrievedChunk
from app.rag.vectorstore import get_vector_store

logger = logging.getLogger(__name__)

#: 向量库未提供标题时的占位标题
_UNKNOWN_TITLE: Final[str] = "未命名文档"
#: metadata 缺少 ``source`` 字段时的占位来源
_UNKNOWN_SOURCE: Final[str] = "未知来源"

#: 零宽字符与 BOM。前端粘贴、文件/管道输入可能带上这些不可见字符，
#: ``str.strip()`` 不会去掉它们，但它们会进入查询向量并拉低检索质量，
#: 因此在入口处剔除（与文档入库时的清洗保持一致）。
_INVISIBLE_CHARS: Final[str] = "\ufeff\u200b\u200c\u200d\u2060"

#: 距离 -> 相关性 的换算函数签名，由向量库按自身距离度量提供
RelevanceScoreFn = Callable[[float], float]


def _as_float(value: object) -> float:
    """把后端返回的数值统一成内建 ``float``。

    各向量库可能返回 ``numpy.float32`` 等类型，直接写入状态会影响后续序列化，
    因此在本层收口。
    """

    return float(value)  # type: ignore[arg-type]


def _resolve_relevance_score_fn(store: VectorStore) -> RelevanceScoreFn | None:
    """取得向量库自带的相关性换算函数。

    走 ``VectorStore`` 的抽象契约 ``_select_relevance_score_fn()``：由后端根据
    自身的距离度量（cosine / l2 / ip）返回对应换算方式，因此本模块不需要知道
    底层是谁。基类默认实现直接 ``raise NotImplementedError``，无法提供时返回
    ``None``，由调用方决定降级策略。
    """

    selector = getattr(store, "_select_relevance_score_fn", None)
    if not callable(selector):
        return None

    try:
        score_fn = selector()
    except Exception as exc:  # noqa: BLE001 - 后端未实现该契约属于预期情况
        logger.debug("向量库未提供相关性换算函数: %s", exc)
        return None

    return score_fn if callable(score_fn) else None


def _sanitize_query(query: str) -> str:
    """规整查询串：去掉首尾空白与不可见字符。

    不可见字符（BOM、零宽空格等）会改变查询向量，即使肉眼看起来一样，
    也会让检索分数发生偏移，因此在进入向量库前统一清理。
    """

    return query.strip().strip(_INVISIBLE_CHARS).strip()


def _rank_key(hit: tuple[Document, float | None]) -> tuple[int, float]:
    """排序键：有分数的在前且越大越靠前，无分数的保持原顺序排在末尾。"""

    score = hit[1]
    return (1, score) if score is not None else (0, 0.0)


class KnowledgeRetriever:
    """知识库检索器。

    与 LangChain 原生 retriever 的区别：返回带来源信息的 ``RetrievedChunk``
    而非裸 ``Document``，并把「分数语义统一」与「结果排序」固定在本层，
    避免每个调用方各自处理后端差异。
    """

    def __init__(
        self,
        store: VectorStore,
        *,
        top_k: int = 4,
        score_threshold: float | None = None,
        relevance_score_fn: RelevanceScoreFn | None = None,
    ) -> None:
        """初始化检索器。

        Args:
            store: 向量库；只要求实现 LangChain ``VectorStore`` 抽象。
            top_k: 默认召回条数。
            score_threshold: 相关性下限，低于该值的片段被丢弃；``None`` 表示不过滤。
            relevance_score_fn: 距离 -> 相关性 的换算函数。省略时向向量库索取；
                仅在测试或需要覆盖后端度量时才显式传入。
        """

        self._store = store
        self._top_k = top_k
        self._score_threshold = score_threshold
        self._relevance_score_fn = relevance_score_fn

    @property
    def top_k(self) -> int:
        """默认召回条数。"""

        return self._top_k

    @property
    def score_threshold(self) -> float | None:
        """当前相关性阈值；``None`` 表示不过滤。"""

        return self._score_threshold

    @classmethod
    def from_settings(
        cls,
        store: VectorStore | None = None,
        settings: RAGSettings | None = None,
    ) -> KnowledgeRetriever:
        """按配置构建检索器，并复用进程内共享的向量库实例。"""

        rag_settings = settings or get_rag_settings()
        return cls(
            store or get_vector_store(),
            top_k=rag_settings.top_k,
            score_threshold=rag_settings.score_threshold,
        )

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        """检索与 ``query`` 最相关的知识片段。

        Args:
            query: 用户问题原文。
            top_k: 覆盖配置中的召回数量。

        Returns:
            按相关性降序排列的片段；查询为空或检索无结果时返回空列表。
            未触发阈值过滤且配置了阈值时，可能出现空列表。
        """

        question = _sanitize_query(query)
        if not question:
            logger.warning("检索查询为空，直接返回空结果")
            return []

        k = top_k or self._top_k
        hits = self._search(question, k)
        if not hits:
            return []

        ranked = sorted(hits, key=_rank_key, reverse=True)
        accepted = [hit for hit in ranked if self._accepts(hit[1])]
        if not accepted:
            logger.info("检索命中 %d 条但均低于阈值 %s", len(ranked), self._score_threshold)

        chunks = [self._to_chunk(document, score) for document, score in accepted]
        logger.info(
            "检索完成: query=%r 命中=%d 召回=%d",
            question[:40],
            len(ranked),
            len(chunks),
        )
        return chunks

    def as_langchain_retriever(self, **search_kwargs: object) -> BaseRetriever:
        """导出 LangChain 标准 retriever，便于后续编排进 Chain / Tool。"""

        kwargs: dict[str, object] = {"k": self._top_k}
        kwargs.update(search_kwargs)
        return self._store.as_retriever(search_kwargs=kwargs)

    # ------------------------------------------------------------------ 内部实现

    def _search(self, query: str, k: int) -> list[tuple[Document, float | None]]:
        """调用向量库检索，返回 ``(文档, 相关性分数)`` 列表。

        分数语义在此统一为「越大越相关」，分三条路径：

        1. 后端直接支持相关性分数 -> 直接采用；
        2. 只能返回原始距离 -> 用后端自带的换算函数在本层转换；
        3. 两者都不可用 -> 分数置 ``None``（语义未知时宁可无分数，也不把距离当成相关性）。
        """

        try:
            scored = self._store.similarity_search_with_relevance_scores(query, k=k)
        except Exception as exc:  # noqa: BLE001 - 部分后端未实现相关性分数契约
            logger.warning("向量库未直接提供相关性分数，转为距离换算: %s", exc)
        else:
            return [(document, _as_float(score)) for document, score in scored]

        try:
            raw_scored = self._store.similarity_search_with_score(query, k=k)
        except Exception:  # noqa: BLE001 - 检索失败不应中断整轮对话
            logger.exception("向量检索失败")
            return []

        to_relevance = self._relevance_score_fn or _resolve_relevance_score_fn(self._store)
        if to_relevance is None:
            logger.warning("无法把距离换算为相关性，本次结果不提供分数")
            return [(document, None) for document, _ in raw_scored]

        return [
            (document, _as_float(to_relevance(_as_float(distance))))
            for document, distance in raw_scored
        ]

    def _accepts(self, score: float | None) -> bool:
        """按阈值过滤。未配置阈值或分数未知时全部保留。"""

        if self._score_threshold is None or score is None:
            return True
        return score >= self._score_threshold

    @staticmethod
    def _to_chunk(document: Document, score: float | None) -> RetrievedChunk:
        """把向量库返回的 ``Document`` 转成统一的 ``RetrievedChunk``。

        ``metadata`` 原样透传（页码、片段序号等），仅在标题上附加页码便于展示。
        """

        metadata = dict(document.metadata)
        source = str(metadata.get("source") or _UNKNOWN_SOURCE)
        title = str(metadata.get("title") or _UNKNOWN_TITLE)
        if metadata.get("page"):
            title = f"{title}（第 {metadata['page']} 页）"
        return RetrievedChunk(
            content=document.page_content,
            source=source,
            title=title,
            score=score,
            metadata=metadata,
        )


__all__ = ["KnowledgeRetriever", "RelevanceScoreFn"]
