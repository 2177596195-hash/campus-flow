"""LEGACY：基于 ``langchain.agents.create_agent`` 的早期 Agent 实现。

保留原因：维持既有的 tool-calling 调用能力，避免功能回退。
主运行时已迁移至 LangGraph（``app.agent.graph``），本模块仅为兼容入口，
新代码请勿依赖；待 Tool Calling 能力并入 LangGraph 图后可整体移除。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain.agents import create_agent

from app.agent.model import get_llm
from app.tools.exam_tools import get_exam_schedule


@lru_cache(maxsize=1)
def get_agent() -> Any:
    """返回惰性构建的 legacy Agent（模型在首次调用时解析）。"""

    return create_agent(
        model=get_llm(),
        tools=[get_exam_schedule],
    )


def __getattr__(name: str) -> Any:
    """向后兼容旧的模块级 ``agent`` 变量。"""

    if name == "agent":
        return get_agent()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
