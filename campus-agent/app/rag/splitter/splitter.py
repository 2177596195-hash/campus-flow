"""切分层：统一的文档 Chunk 策略。

切分参数全部来自 ``RAGSettings``（``RAG_CHUNK_SIZE`` / ``RAG_CHUNK_OVERLAP``），
本模块内不存在任何魔法数字，换策略只需替换 ``create_text_splitter`` 的实现。

分隔符顺序针对中文校园文档调优：优先在 Markdown 章节边界切开，
其次段落，再退到中文句读，最后才按字符硬切，尽量保证片段语义完整。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config.settings import RAGSettings, get_rag_settings

logger = logging.getLogger(__name__)

#: 分隔符优先级：Markdown 章节 > 段落 > 中文句读 > 空白 > 字符
_SEPARATORS: tuple[str, ...] = (
    "\n## ",
    "\n### ",
    "\n#### ",
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    "，",
    " ",
    "",
)


def create_text_splitter(settings: RAGSettings | None = None) -> RecursiveCharacterTextSplitter:
    """按配置构建切分器。

    ``add_start_index=True`` 会为每个片段写入 ``start_index``，
    便于排查「某个片段来自原文哪一段」。
    """

    rag_settings = settings or get_rag_settings()
    logger.debug(
        "切分策略: chunk_size=%d chunk_overlap=%d",
        rag_settings.chunk_size,
        rag_settings.chunk_overlap,
    )
    return RecursiveCharacterTextSplitter(
        chunk_size=rag_settings.chunk_size,
        chunk_overlap=rag_settings.chunk_overlap,
        separators=list(_SEPARATORS),
        length_function=len,
        add_start_index=True,
        strip_whitespace=True,
    )


def split_documents(
    documents: Sequence[Document],
    splitter: RecursiveCharacterTextSplitter | None = None,
) -> list[Document]:
    """切分文档，并为每个片段补充 ``chunk_index``（同一来源内从 0 递增）。

    相同 chunk_size / chunk_overlap 下切分结果稳定，
    因此同一份资料的 ``chunk_index`` 在多次摄取之间保持一致。
    """

    active_splitter = splitter or create_text_splitter()
    counters: dict[str, int] = {}
    chunks: list[Document] = []

    for chunk in active_splitter.split_documents(list(documents)):
        source = str(chunk.metadata.get("source", ""))
        index = counters.get(source, 0)
        counters[source] = index + 1
        chunk.metadata["chunk_index"] = index
        chunks.append(chunk)

    logger.info("切分完成: 文档 %d 个 -> 片段 %d 个", len(documents), len(chunks))
    return chunks


__all__ = ["create_text_splitter", "split_documents"]
