"""知识库文档加载与清洗。"""

from app.rag.loader.cleaners import clean_documents, clean_text
from app.rag.loader.loaders import (
    DocumentLoader,
    PdfDocumentLoader,
    TextDocumentLoader,
    UnsupportedDocumentError,
    iter_document_files,
    load_directory,
    load_document,
    load_files,
    loader_for,
    supported_suffixes,
)

__all__ = [
    "DocumentLoader",
    "PdfDocumentLoader",
    "TextDocumentLoader",
    "UnsupportedDocumentError",
    "clean_documents",
    "clean_text",
    "iter_document_files",
    "load_directory",
    "load_document",
    "load_files",
    "loader_for",
    "supported_suffixes",
]
