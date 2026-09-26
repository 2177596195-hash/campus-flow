"""文档清洗层：加载之后、切分之前的内容规整。

目标只有一个——让进入向量库的文本干净且不重复。因此这里只做
无损的噪声处理（换行、控制字符、多余空白）与去重，
**不做摘要、不改写、不删除语义内容**，避免污染检索结果。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Sequence

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

#: 需要剔除的不可见控制字符（保留 \n 与 \t）
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\ufeff]")
#: 行尾空白
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)
#: 连续 3 个以上换行压缩为 2 个（保留段落分隔）
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")
#: 连续空格压缩（PDF 抽取常见问题）
_EXCESS_SPACES_RE = re.compile(r"[ \t]{3,}")
#: 全角空格
_IDEOGRAPHIC_SPACE: str = "\u3000"


def clean_text(text: str) -> str:
    """规整单段文本，返回可入库的干净内容。

    归一化使用 NFC 而非 NFKC：NFKC 会把中文全角标点（，；：）与全角字母
    折叠为 ASCII，破坏中文排版并使检索片段难以阅读。NFC 只做等价组合，
    不影响语义相同的字符。
    """

    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace(_IDEOGRAPHIC_SPACE, " ")
    normalized = _CONTROL_CHARS_RE.sub("", normalized)
    normalized = _TRAILING_SPACE_RE.sub("", normalized)
    normalized = _EXCESS_SPACES_RE.sub("  ", normalized)
    normalized = _EXCESS_NEWLINES_RE.sub("\n\n", normalized)
    return normalized.strip()


def _content_fingerprint(text: str) -> str:
    """内容指纹，用于跨文件去重。"""

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def clean_documents(documents: Sequence[Document]) -> list[Document]:
    """批量清洗文档：规整文本、丢弃空文档、按内容去重。

    去重是有意的：同一份资料被重复放入知识库时，
    重复片段会挤占 ``top_k`` 名额并降低检索质量。
    """

    cleaned: list[Document] = []
    seen: set[str] = set()

    for document in documents:
        text = clean_text(document.page_content)
        if not text:
            logger.debug("清洗后内容为空，已丢弃: %s", document.metadata.get("source"))
            continue

        fingerprint = _content_fingerprint(text)
        if fingerprint in seen:
            logger.info("内容重复，已去重: %s", document.metadata.get("source"))
            continue
        seen.add(fingerprint)

        cleaned.append(Document(page_content=text, metadata=dict(document.metadata)))

    return cleaned


__all__ = ["clean_documents", "clean_text"]
