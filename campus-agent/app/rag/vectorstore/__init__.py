"""向量库工厂。

对外统一返回 LangChain ``VectorStore`` 抽象，上层不感知具体后端。
切换到 Milvus 的步骤见 ``factory.py`` 模块文档。
"""

from app.rag.vectorstore.factory import (
    DEFAULT_BACKEND,
    VectorStoreError,
    count_documents,
    create_vector_store,
    delete_chunks,
    get_vector_store,
    reset_collection,
    supported_backends,
)

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
