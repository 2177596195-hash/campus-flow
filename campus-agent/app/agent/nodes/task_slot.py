"""任务槽位节点：把「上一轮缺槽位的待办任务」和「本轮的补充信息」对上。

职责边界：本节点只做三件事——

1. 读取状态里的待办任务（``pending_intent`` / ``pending_slot``）与本轮用户输入；
2. 能补齐槽位就补齐：清空待办、把值写回 ``pending_value``，并把 ``intent``
   恢复成待办任务的意图，让下游 Agent 接着把原任务做完；
3. 补不齐时判断「本轮是否产生了新的缺槽位任务」，产生则写入待办并给出追问。

它**不调用模型、不执行工具、不发网络请求**。追问是确定性的（缺哪个槽位就问
哪一句，文案在 ``app.agent.slots``），因此「缺槽位」这一步不会白白花掉一次
模型调用；真正的取数仍由 ``agent`` + 官方 ``ToolNode`` 完成，Tool Calling
链路一个环节都没有被绕开。

本节点为什么必要
----------------

图里的意图识别是**每轮独立**的：第二轮用户只说「上学期」，孤立地看既不是
查成绩也不是任何已知意图。任务状态补上的正是这一块——待办里记着「上一轮在查
成绩、缺学期」，于是本轮输入被解释为「给那个未完成的任务补一个学期」，
而不是被当成一句新的话。这也是本阶段「多轮任务状态」的全部意义。

请假为什么走同一条机制、但语义不同
----------------------------------

请假（``action``）与查成绩（``query``）共用 ``pending_intent`` /
``pending_slot`` / ``pending_value`` 三个字段，但补齐后的处理刻意不同：

- **追问是确定的，取值不是**：缺哪个槽位就问哪一句，这部分两者一致；但学期
  可以无损归一化成 ``2025-2026-1``，请假要的是「起止时间 + 事由」这份**自由
  文本**，确定性地解析它需要一整套自然语言日期规则，解析错就直接变成一条错
  申请。因此这里**原样**把用户的话记进 ``pending_value``（带 ``leave_detail:``
  前缀，见 :mod:`app.agent.slots`），字段提取交给模型在 ``tool_calls`` 里完成。
- **本节点不做任何业务判断**：既不算请假天数、不判断合规性，也不拼工具参数。
  它只负责「上一轮缺的补充信息，这一轮收到了没有」。
- **不新增状态字段**：槽位值的归属由值自身的前缀决定，故 ``AgentState``
  仍是原来的 9 个字段，查成绩那条路径一个字节都没变。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.agent.graph.state import AgentState
from app.agent.nodes.intent_router import latest_user_text
from app.agent.slots import (
    SLOT_LEAVE,
    SLOT_SEMESTER,
    encode_leave_detail,
    missing_slot,
    resolve_semester,
    slot_question,
)

logger = logging.getLogger(__name__)

TaskSlotNode = Callable[[AgentState, Optional[RunnableConfig]], dict[str, Any]]

#: 「自己就能作答」的分支：待办任务遇到它们即视为已被用户放弃。
#:
#: 为什么需要这条规则：待办不清空的话，「等我补学期」这件事会一直挂在那儿，
#: 之后每一轮都会被重新追问一次——那是比不实现多轮更糟的回归。
#: 因此只在**本轮确实没补上槽位**、而本轮意图又明显是另一件独立的事
#: （闲聊 / 知识问答 / 要执行操作）时取消待办。
#: ``query`` 与 ``unknown`` 不算独立事件：前者是用户在重问同一个问题，
#: 后者（如「嗯……」）信息量不足以判断他换了话题，都继续追问。
#:
#: 注意：这条只在「槽位解析不出值」时才生效，而请假槽位接受任意非空文本
#: （见 ``_resolve_slot``），因此**请假补充信息永远不会触发取消**——
#: 用户回答「10月12日到10月14日，感冒」时，孤立识别的意图很可能是 unknown
#: 或 action，若被当成「换了话题」就会把补充信息丢掉。
SELF_CONTAINED_INTENTS: Final[frozenset[str]] = frozenset({"chat", "knowledge", "action"})


def _resolve_slot(slot: str, text: str) -> str | None:
    """按槽位名解析本轮的补充信息；解析不出返回 ``None``。

    当前是两个槽位的小分派表：新增槽位时在此登记对应的解析函数，
    而不是把逻辑塞进节点主干。
    """

    if slot == SLOT_SEMESTER:
        return resolve_semester(text)
    if slot == SLOT_LEAVE:
        return encode_leave_detail(text)
    logger.warning("槽位 %s 尚未登记解析方式，按「未补齐」处理", slot)
    return None


def create_task_slot_node() -> TaskSlotNode:
    """构建任务槽位节点。

    Returns:
        符合 LangGraph 节点签名的可调用对象。
    """

    def task_slot_node(
        state: AgentState, config: Optional[RunnableConfig] = None
    ) -> dict[str, Any]:
        """补齐待办槽位，或为缺少槽位的新请求写入待办并追问。

        注：``config`` 的注解需保持 ``Optional[RunnableConfig]`` 这一写法，
        LangGraph 的节点签名校验按字符串比对，使用 ``RunnableConfig | None``
        会触发 UserWarning。本节点不读 ``config``，留参只为保持节点签名一致。
        """

        text = latest_user_text(state)
        intent = state.get("intent")
        pending_slot = state.get("pending_slot")

        if pending_slot:
            value = _resolve_slot(pending_slot, text)
            if value is not None:
                logger.info(
                    "槽位补齐: %s=%s -> 继续任务 %s",
                    pending_slot,
                    value,
                    state.get("pending_intent"),
                )
                return {
                    # 任务上下文优先于本轮孤立识别的结果：本轮的「上学期」
                    # 单独看什么都不是，是待办任务给了它含义。
                    "intent": state.get("pending_intent") or intent,
                    "pending_slot": None,
                    "pending_value": value,
                    "error": None,
                }

            if intent in SELF_CONTAINED_INTENTS:
                logger.info(
                    "待办任务 %s 被新的 %s 请求取代，取消槽位等待",
                    state.get("pending_intent"),
                    intent,
                )
                return {
                    "pending_intent": None,
                    "pending_slot": None,
                    "pending_value": None,
                    "error": None,
                }

            logger.info("槽位 %s 仍未补齐，继续追问", pending_slot)
            return {
                "messages": [AIMessage(content=slot_question(pending_slot))],
                "error": None,
            }

        slot = missing_slot(text, intent)
        if slot is not None:
            logger.info("本轮请求缺少槽位 %s，写入待办并追问", slot)
            return {
                "pending_intent": intent,
                "pending_slot": slot,
                "pending_value": None,
                "messages": [AIMessage(content=slot_question(slot))],
                "error": None,
            }

        return {"error": None}

    return task_slot_node


__all__ = ["SELF_CONTAINED_INTENTS", "TaskSlotNode", "create_task_slot_node"]
