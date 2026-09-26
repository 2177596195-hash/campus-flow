"""文档加载层：把各类原始文件统一转换为 LangChain ``Document``。

设计约定：
1. 一种格式对应一个 ``DocumentLoader`` 实现，通过后缀登记表自动分派；
   新增格式（docx / html / excel 等）只需实现协议并登记，调用方无需改动。
2. 本层只做「抽取文本 + 补充来源 metadata」，不做清洗与切分。
3. 不感知向量库、Embedding 与图拓扑。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

#: 忽略的目录名（版本控制、缓存、虚拟环境等）
_IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".storage", ".idea", ".vscode", "node_modules"}
)

#: Markdown 一级标题正则（仅用于提取引用标题）
_MD_TITLE_PREFIX: str = "# "

#: 纯文本类文件后缀
_TEXT_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".text"})
#: PDF 后缀
_PDF_SUFFIXES: frozenset[str] = frozenset({".pdf"})


class UnsupportedDocumentError(ValueError):
    """文件格式不受支持。"""


class DocumentLoader(Protocol):
    """文档加载器协议。"""

    #: 该加载器负责的文件后缀（小写，含点号）
    suffixes: frozenset[str]
    #: 加载器名称，写入 metadata 便于排查
    name: str

    def load(self, path: Path) -> list[Document]:
        """读取文件并返回一个或多个 ``Document``。"""

        ...


def _read_text(path: Path) -> str:
    """读取文本文件；编码异常时降级为替换模式，避免单个坏字节中断摄取。"""

    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.warning("文件 %s 非 UTF-8 编码，已降级为宽松解码", path.name)
        return path.read_text(encoding="utf-8", errors="replace")


def _extract_markdown_title(text: str, fallback: str) -> str:
    """提取首个 Markdown 一级标题作为文档标题，缺失时回退到文件名。"""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_MD_TITLE_PREFIX):
            return stripped[len(_MD_TITLE_PREFIX) :].strip() or fallback
        # 首个非空行不是一级标题，说明该文档没有可用标题
        break
    return fallback


class TextDocumentLoader:
    """Markdown / 纯文本加载器。"""

    suffixes = _TEXT_SUFFIXES
    name = "text"

    def load(self, path: Path) -> list[Document]:
        text = _read_text(path)
        if not text.strip():
            logger.warning("文件 %s 内容为空，已跳过", path.name)
            return []

        return [
            Document(
                page_content=text,
                metadata={
                    "title": _extract_markdown_title(text, path.stem),
                    "suffix": path.suffix.lower(),
                    "loader": self.name,
                },
            )
        ]


class PdfDocumentLoader:
    """PDF 加载器：逐页抽取文本，保留页码 metadata。

    仅使用 ``pypdf`` 这一轻量依赖，不引入 langchain-community 全量集成包；
    后续若需要版面还原（表格、图片 OCR），可替换为 MinerU / unstructured 实现，
    替换点仅在本文件内部。
    """

    suffixes = _PDF_SUFFIXES
    name = "pdf"

    def load(self, path: Path) -> list[Document]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
            raise UnsupportedDocumentError(
                "PDF 支持需要安装 pypdf：uv add pypdf"
            ) from exc

        reader = PdfReader(str(path))
        title = path.stem
        pdf_title = (reader.metadata or {}).get("/Title")
        if isinstance(pdf_title, str) and pdf_title.strip():
            title = pdf_title.strip()

        documents: list[Document] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - 单页损坏不应中断整份文档
                logger.warning("PDF %s 第 %d 页解析失败，已跳过", path.name, page_number, exc_info=True)
                continue
            if not text.strip():
                continue
            documents.append(
                Document(
                    page_content=text,
                    metadata={
                        "title": title,
                        "page": page_number,
                        "suffix": path.suffix.lower(),
                        "loader": self.name,
                    },
                )
            )

        if not documents:
            logger.warning("PDF %s 未抽取到任何文本（可能为扫描件）", path.name)
        return documents


#: 已登记的加载器，按后缀命名空间互相独立
_LOADERS: tuple[DocumentLoader, ...] = (TextDocumentLoader(), PdfDocumentLoader())

#: 后缀 -> 加载器 的分派表
_LOADER_BY_SUFFIX: dict[str, DocumentLoader] = {
    suffix: loader for loader in _LOADERS for suffix in loader.suffixes
}


def supported_suffixes() -> tuple[str, ...]:
    """返回全部受支持的文件后缀。"""

    return tuple(sorted(_LOADER_BY_SUFFIX))


def loader_for(path: Path) -> DocumentLoader:
    """返回处理该文件的加载器。

    Raises:
        UnsupportedDocumentError: 后缀未登记。
    """

    loader = _LOADER_BY_SUFFIX.get(path.suffix.lower())
    if loader is None:
        supported = ", ".join(supported_suffixes())
        raise UnsupportedDocumentError(
            f"不支持的文件格式 {path.suffix or '(无后缀)'}（{path.name}），受支持: {supported}"
        )
    return loader


def load_document(path: Path, *, source: str | None = None) -> list[Document]:
    """加载单个文件。

    Args:
        path: 文件路径。
        source: 写入 metadata 的来源标识；省略时使用文件名。

    Returns:
        该文件对应的 ``Document`` 列表；空文件返回空列表。
    """

    documents = loader_for(path).load(path)
    source_name = source or path.name
    for document in documents:
        document.metadata["source"] = source_name
    return documents


def iter_document_files(root: Path, *, recursive: bool = True) -> Iterator[Path]:
    """遍历目录下受支持的文件（忽略隐藏文件与缓存目录），按路径排序保证摄取结果稳定。"""

    if not root.is_dir():
        return

    candidates: Iterable[Path] = root.rglob("*") if recursive else root.glob("*")
    files = [
        candidate
        for candidate in candidates
        if candidate.is_file()
        and not candidate.name.startswith(".")
        and not _IGNORED_DIR_NAMES.intersection(candidate.relative_to(root).parts)
        and candidate.suffix.lower() in _LOADER_BY_SUFFIX
    ]
    yield from sorted(files)


def load_files(root: Path, paths: Iterable[Path]) -> tuple[list[Document], list[str]]:
    """加载指定的一组文件。

    增量索引只处理发生变更的文档，因此需要按路径集合加载。
    与 ``load_directory`` 共用同一条单文件处理路径，
    保证「跳过原因」的措辞与日志完全一致。

    Args:
        root: 知识库根目录，用于把路径换算成稳定的相对 ``source``。
        paths: 待加载的文件路径。

    Returns:
        ``(documents, skipped)``：成功加载的文档与跳过原因列表。
    """

    documents: list[Document] = []
    skipped: list[str] = []

    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            loaded = load_document(path, source=relative)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其余文档
            logger.warning("加载失败，已跳过: %s (%s)", relative, exc)
            skipped.append(f"{relative}: {exc}")
            continue

        if not loaded:
            skipped.append(f"{relative}: 未抽取到文本")
            continue
        documents.extend(loaded)

    return documents, skipped


def load_directory(
    root: Path, *, recursive: bool = True
) -> tuple[list[Document], list[str]]:
    """加载目录下的全部受支持文件。

    Returns:
        ``(documents, skipped)``：成功加载的文档与跳过原因列表。
        单个文件失败不会中断整体加载。
    """

    if not root.is_dir():
        logger.warning("知识库目录不存在: %s", root)
        return [], []

    documents, skipped = load_files(root, iter_document_files(root, recursive=recursive))

    logger.info("知识库目录 %s 加载完成: 文档 %d 个，跳过 %d 个", root, len(documents), len(skipped))
    return documents, skipped


__all__ = [
    "DocumentLoader",
    "PdfDocumentLoader",
    "TextDocumentLoader",
    "UnsupportedDocumentError",
    "iter_document_files",
    "load_directory",
    "load_document",
    "load_files",
    "loader_for",
    "supported_suffixes",
]
