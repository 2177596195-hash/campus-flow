"""向量库工厂。

职责边界：只回答「向量存到哪里」，对外统一返回 LangChain 标准的
``VectorStore`` 抽象，因此检索器与 RAG Service 不知道底层是 Chroma 还是 Milvus。

当前后端：``chroma``（本地持久化，适合开发与单机部署）
- 使用 ``persist_directory`` 落盘，进程重启后可直接加载，无需重新向量化。
- 支持 metadata 过滤，满足按来源 / 页码筛选的后续需求。

切换到 Milvus（生产方案）的完整步骤：
1. 安装官方集成包：``uv add langchain-milvus pymilvus``；
2. 在 ``_VECTORSTORE_FACTORIES`` 中登记一行::

       "milvus": _create_milvus,

   其中 ``_create_milvus`` 只需 ``Milvus(embedding_function=..., connection_args=...,
   collection_name=...)``，返回同样的 ``VectorStore``；
3. 设置 ``VECTORSTORE_BACKEND=milvus`` 与 ``RAG_COLLECTION``。

检索器、RAG Service、knowledge 节点与图拓扑**均无需改动**。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Final

from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore

from app.config.settings import RAGSettings, get_rag_settings
from app.rag.embeddings import get_embeddings

logger = logging.getLogger(__name__)

#: 未显式指定 VECTORSTORE_BACKEND 时使用的后端
DEFAULT_BACKEND: Final[str] = "chroma"

#: Chroma 距离度量：余弦空间下相关性分数等价于余弦相似度，便于设置阈值
_COLLECTION_METADATA: Final[dict[str, str]] = {"hnsw:space": "cosine"}


class VectorStoreError(RuntimeError):
    """向量库后端配置非法或不可用。"""


def _create_chroma(
    embeddings: Embeddings,
    *,
    persist_dir: Path,
    collection_name: str,
) -> VectorStore:
    """构建 Chroma 持久化向量库。"""

    try:
        from langchain_chroma import Chroma
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
        raise VectorStoreError(
            "Chroma 后端需要安装 langchain-chroma：uv add langchain-chroma"
        ) from exc

    persist_dir.mkdir(parents=True, exist_ok=True)
    store = Chroma(
        collection_name=collection_name,
        embedding_function=embeddings,
        persist_directory=str(persist_dir),
        collection_metadata=dict(_COLLECTION_METADATA),
    )
    logger.info("向量库就绪: chroma collection=%s dir=%s", collection_name, persist_dir)
    return store


#: 后端名 -> 构建函数。新增 Milvus 等实现时在此登记即可，上游零改动。
_VECTORSTORE_FACTORIES: Final[
    dict[str, Callable[..., VectorStore]]
] = {
    "chroma": _create_chroma,
}


def supported_backends() -> tuple[str, ...]:
    """返回已登记的向量库后端名。"""

    return tuple(sorted(_VECTORSTORE_FACTORIES))


def create_vector_store(
    backend: str | None = None,
    *,
    settings: RAGSettings | None = None,
    embeddings: Embeddings | None = None,
) -> VectorStore:
    """按配置构建向量库。

    Args:
        backend: 后端名；省略时取配置中的 ``VECTORSTORE_BACKEND``。
        settings: RAG 配置；省略时读取全局配置。
        embeddings: 向量化模型；省略时使用全局共享实例。

    Raises:
        VectorStoreError: 后端名未登记。
    """

    rag_settings = settings or get_rag_settings()
    name = (backend or rag_settings.vectorstore_backend).strip().lower()
    factory = _VECTORSTORE_FACTORIES.get(name)
    if factory is None:
        supported = ", ".join(supported_backends())
        raise VectorStoreError(f"不支持的 VECTORSTORE_BACKEND: {name!r}，可选值: {supported}")

    return factory(
        embeddings or get_embeddings(),
        persist_dir=rag_settings.persist_dir,
        collection_name=rag_settings.collection_name,
    )


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    """返回进程内共享的向量库实例（惰性构建，导入期不读取配置、不建连接）。"""

    return create_vector_store()


def count_documents(store: VectorStore) -> int:
    """统计向量库中的片段数量，用于判断是否需要首次摄取。

    优先使用后端的原生计数（避免全量拉取），失败时降级为按 id 列表计数。
    """

    collection = getattr(store, "_collection", None)
    native_count = getattr(collection, "count", None)
    if callable(native_count):
        try:
            return int(native_count())
        except Exception:  # noqa: BLE001 - 后端差异，降级处理
            logger.debug("原生计数不可用，降级为按 id 计数", exc_info=True)

    try:
        return len(store.get(limit=None)["ids"])
    except Exception:  # noqa: BLE001 - 空库或后端差异，保守返回 0
        logger.debug("统计向量库片段数失败，按空库处理", exc_info=True)
        return 0


def reset_collection(store: VectorStore) -> None:
    """清空当前集合（重建知识库时使用）。"""

    reset = getattr(store, "reset_collection", None)
    if callable(reset):
        reset()
        logger.info("向量库集合已重置")
        return

    ids = store.get(limit=None)["ids"]
    if ids:
        store.delete(ids=ids)
    logger.info("向量库集合已清空（逐条删除 %d 个片段）", len(ids))


def delete_chunks(store: VectorStore, ids: Sequence[str]) -> int:
    """按片段 id 删除向量，返回提交删除的片段数。

    只使用 ``VectorStore.delete(ids=...)`` 这一标准契约，不依赖任何后端的
    metadata 过滤能力，因此增量索引的「按 source 删除」与后端无关：
    调用方从索引清单中取出该 source 的片段 id 即可精确定位。

    删除不存在的 id 在后端上通常是无副作用的空操作，故此处不区分
    「删除成功」与「本来就不存在」，两者都表示目标状态已达成。
    """

    unique = [str(item) for item in dict.fromkeys(ids)]
    if not unique:
        return 0

    try:
        store.delete(ids=unique)
    except Exception:  # noqa: BLE001 - 后端差异不应中断整轮索引
        logger.exception("按 id 删除片段失败（%d 个），向量库可能残留旧片段", len(unique))
        return 0

    logger.info("已清理旧片段 %d 个", len(unique))
    return len(unique)


__all__ = [
    "DEFAULT_BACKEND",
    "VectorStoreError",
    "count_documents",
    "create_vector_store",
    "delete_chunks",
    "get_vector_store",
    "reset_collection",
    "supported_backends",
]
