"""Agent Runtime 的共享状态契约。

状态是图的唯一数据契约：节点只读写此处声明的字段。
后续扩展 RAG / Tool Calling / MCP / Memory / Human-in-the-loop 时，
在本文件追加字段即可，既有节点无需改动（无侵入扩展）。
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.agent.intent import Intent


class AgentState(TypedDict):
    """图运行期状态。

    ``messages`` 使用 ``add_messages`` reducer：节点只需返回增量消息，
    由 reducer 负责按消息 id 去重合并 / 追加；挂载 checkpointer 后，
    同一 ``thread_id`` 的多轮消息会在该 reducer 下持续累积。

    其余字段为可选扩展槽位，未赋值时不影响图运行。
    """

    messages: Annotated[list[AnyMessage], add_messages]

    # ---- 会话与意图 ----
    # 意图路由结果：由 intent_router 节点写入，供条件边选择下游分支
    intent: NotRequired[Intent]
    # 会话标识：与 checkpointer 的 thread_id 对应，用于多轮上下文隔离
    session_id: NotRequired[str]
    # 用户标识：后续接入鉴权与个性化时使用
    user_id: NotRequired[str]
    # 最近一次节点执行失败的原因，正常为 None
    error: NotRequired[str | None]

    # ---- 知识库检索 ----
    # 引用来源列表，元素结构见 app.rag.schema.RetrievedChunk.as_source：
    # {"source": 相对路径, "title": 标题, "score": 相关性, "snippet": 片段摘要}
    # 由 knowledge 节点写入，供前端展示引用出处。
    sources: NotRequired[list[dict[str, Any]]]

    # ---- 多轮任务上下文（槽位填充）----
    # 下列三个字段共同描述「一个尚未完成的任务」。它们随 checkpointer 一起
    # 按 thread_id 保存，因此下一轮能认出「用户这句话是在补充上一个任务」，
    # 而不是当成一句全新的话——这正是每轮独立识别意图做不到的事。
    #
    # 待办任务的意图；为 None 表示当前没有未完成的任务。
    pending_intent: NotRequired[Intent | None]
    # 待补齐的槽位名（如 "semester"）；为 None 表示没有缺槽位。
    # 由 task_slot 节点写入与清空，也是条件边判断「本轮是否只输出追问」的依据。
    pending_slot: NotRequired[str | None]
    # 上一轮补齐的槽位**值**；由 task_slot 节点写入，agent 节点消费一次即置回
    # None（避免它在后续轮次里被反复当成新提示）。
    pending_value: NotRequired[str | None]

