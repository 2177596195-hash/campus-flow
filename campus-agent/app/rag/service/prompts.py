"""RAG 问答提示词。

与代码逻辑分离：调整回答风格、引用格式时只改本文件，不触碰服务实现。
提示词中不包含任何具体学校数据——知识内容一律来自向量库检索结果。
"""

from __future__ import annotations

#: 无检索结果时的兜底回复
NO_CONTEXT_REPLY: str = (
    "知识库中暂时没有找到与该问题相关的资料。"
    "若确认知识库已导入相关文件，可以换个说法再问一次；"
    "涉及个人成绩、课表等数据请改用查询类问题。"
)

#: 模型调用失败时的兜底回复
RAG_FALLBACK_REPLY: str = "抱歉，知识库检索服务暂时不可用，请稍后重试。"

#: 上下文片段模板：编号 + 来源，便于模型按编号引用
CONTEXT_BLOCK_TEMPLATE: str = "[{index}] 来源：{title}（{source}）\n{content}"

RAG_SYSTEM_PROMPT: str = (
    "你是校园事务知识助手，负责依据给定的知识库资料回答师生的问题。\n"
    "必须遵守的规则：\n"
    "1. 只使用下方【知识库资料】中的信息作答，不要引入资料之外的事实。\n"
    "2. 资料足以回答时，用简洁的中文给出现行做法与关键条件。\n"
    "3. 资料不足或与问题无关时，明确说明「知识库中未找到相关规定」，不要猜测、不要编造。\n"
    "4. 若资料之间存在冲突，指出冲突并优先采用生效日期较新的说明。\n"
    "5. 引用资料时在句末标注来源编号，例如「……[1]」；"
    "不要另起一行列出参考来源清单，引用清单由调用方根据 metadata 统一渲染。\n"
    "6. 涉及个人数据（成绩、课表、学籍等）的问题，说明这类查询需要走教务系统，不能凭资料推断。\n"
)

# 注：规则 5 中的「来源编号」由 CONTEXT_BLOCK_TEMPLATE 写入，两者需同步修改。


def build_context(chunks: list[tuple[int, str, str, str]]) -> str:
    """拼装知识库资料块。

    Args:
        chunks: ``(编号, 标题, 来源, 内容)`` 四元组列表。

    Returns:
        形如 ``【知识库资料】\\n[1] 来源：...`` 的完整段落。
    """

    blocks = "\n\n".join(
        CONTEXT_BLOCK_TEMPLATE.format(index=index, title=title, source=source, content=content)
        for index, title, source, content in chunks
    )
    return f"【知识库资料】\n{blocks}"


__all__ = [
    "CONTEXT_BLOCK_TEMPLATE",
    "NO_CONTEXT_REPLY",
    "RAG_FALLBACK_REPLY",
    "RAG_SYSTEM_PROMPT",
    "build_context",
]
