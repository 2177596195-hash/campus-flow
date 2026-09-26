"""RAG 链路的数据契约。

本模块只定义跨层传递的数据结构，不含任何检索或模型调用逻辑，
因此可以被加载器、检索器、服务与节点安全共享。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: 引用片段在 ``AgentState.sources`` 中的最大字符数，供前端做摘要展示
SNIPPET_MAX_CHARS: int = 200


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """一次检索命中的知识片段。"""

    content: str
    #: 来源文件（相对知识库根目录的路径），用于展示引用出处
    source: str
    #: 来源标题：Markdown 取首个一级标题，其余取文件名
    title: str
    #: 相关性分数，越大越相关；向量库不支持时为 ``None``
    score: float | None = None
    #: 原始 metadata（页码、片段序号等），透传不做裁剪
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def page(self) -> int | None:
        """来源页码；Markdown / TXT 这类非分页文档为 ``None``。"""

        raw = self.metadata.get("page")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def as_source(self) -> dict[str, Any]:
        """转换为可序列化的引用结构，写入 ``AgentState.sources``。"""

        snippet = self.content.strip()
        if len(snippet) > SNIPPET_MAX_CHARS:
            snippet = f"{snippet[:SNIPPET_MAX_CHARS]}..."

        return {
            "source": self.source,
            "title": self.title,
            "score": round(self.score, 4) if self.score is not None else None,
            "snippet": snippet,
        }


@dataclass(frozen=True, slots=True)
class RAGAnswer:
    """RAG 服务的问答结果。"""

    query: str
    answer: str
    #: 本次回答依据的知识片段引用（已序列化，可直接返回前端）
    sources: tuple[dict[str, Any], ...] = ()
    #: 检索到的片段数量，便于观测与排查
    retrieved: int = 0

    @property
    def has_context(self) -> bool:
        """是否检索到可用上下文。"""

        return self.retrieved > 0


@dataclass(frozen=True, slots=True)
class IngestReport:
    """知识库索引结果（增量索引与全量重建共用同一契约）。"""

    #: 本次实际写入 / 重写的文档数（= NEW + CHANGED 中读取成功者）
    documents: int = 0
    #: 本次实际写入的片段数
    chunks: int = 0
    #: 索引完成后库内片段总数
    total_chunks: int = 0
    #: 以下四项为变更计划识别到的文档数（非写入成功数）
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    #: CHANGED / DELETED 时从向量库清理掉的旧片段数
    removed_chunks: int = 0
    skipped: tuple[str, ...] = ()
    #: True 表示本次执行的是全量重建
    rebuilt: bool = False
    #: False 表示复用了已有知识库，本次未执行实际索引
    performed: bool = True

    @property
    def pending(self) -> int:
        """本次需要写入向量的文档数。"""

        return self.new + self.changed

    def describe(self) -> str:
        """返回适合日志展示的单行摘要。"""

        if not self.performed:
            return f"知识库已就绪，跳过索引（库内片段 {self.total_chunks} 个）"

        action = "全量重建" if self.rebuilt else "增量索引"
        summary = (
            f"知识库{action}完成：NEW {self.new} / CHANGED {self.changed} / "
            f"UNCHANGED {self.unchanged} / DELETED {self.deleted}，"
            f"写入文档 {self.documents} 个、片段 {self.chunks} 个，"
            f"库内合计 {self.total_chunks} 个"
        )
        if self.removed_chunks:
            summary += f"，清理旧片段 {self.removed_chunks} 个"
        if self.skipped:
            summary += f"，跳过 {len(self.skipped)} 个：{'; '.join(self.skipped)}"
        return summary

    def format_report(self) -> str:
        """返回命令行展示用的多行索引报告。

        前五行是固定的状态统计（便于运维脚本按行解析），
        其余行只在出现对应情况时才输出。
        """

        lines = [
            f"NEW: {self.new}",
            f"CHANGED: {self.changed}",
            f"UNCHANGED: {self.unchanged}",
            f"DELETED: {self.deleted}",
            f"TOTAL_CHUNKS: {self.total_chunks}",
        ]
        if self.removed_chunks:
            lines.append(f"REMOVED_CHUNKS: {self.removed_chunks}")
        if self.rebuilt:
            lines.append("REBUILT: true")
        if not self.performed:
            lines.append("PERFORMED: false")
        lines.extend(f"SKIPPED: {reason}" for reason in self.skipped)
        return "\n".join(lines)
