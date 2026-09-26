"""向量化模型（Embedding）工厂。"""

from app.rag.embeddings.factory import EmbeddingError, build_embeddings, get_embeddings

__all__ = ["EmbeddingError", "build_embeddings", "get_embeddings"]
