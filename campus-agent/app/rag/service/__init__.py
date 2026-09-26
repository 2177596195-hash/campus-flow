"""RAG 服务层：检索问答的唯一对外门面。"""

from app.rag.service.prompts import NO_CONTEXT_REPLY, RAG_FALLBACK_REPLY, RAG_SYSTEM_PROMPT
from app.rag.service.rag_service import (
    LLMProvider,
    RAGService,
    build_rag_service,
    get_rag_service,
    resolve_default_llm,
)

__all__ = [
    "LLMProvider",
    "NO_CONTEXT_REPLY",
    "RAG_FALLBACK_REPLY",
    "RAG_SYSTEM_PROMPT",
    "RAGService",
    "build_rag_service",
    "get_rag_service",
    "resolve_default_llm",
]
