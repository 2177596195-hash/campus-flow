"""意图（Intent）定义与识别。

职责边界：本模块只回答「用户这句话想做什么」，
不涉及路由跳转，也不执行任何业务动作。

识别器以 ``IntentClassifier`` 协议暴露，图侧只依赖该抽象，
因此后续可无侵入替换为规则版、微调小模型版等实现。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal, Protocol, cast, get_args

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from app.agent.model import get_llm

logger = logging.getLogger(__name__)

#: 意图取值。新增意图需同时在本类型、INTENTS 与 graph.py 的 INTENT_NODE_MAP 中登记。
Intent = Literal["chat", "knowledge", "query", "action", "unknown"]

#: 全部合法意图
INTENTS: tuple[Intent, ...] = cast("tuple[Intent, ...]", get_args(Intent))

#: 无法判定或识别异常时的兜底意图
DEFAULT_INTENT: Intent = "unknown"

_INTENT_SET: frozenset[str] = frozenset(INTENTS)

#: 结构化输出方式。必须显式指定：ChatOpenAI 默认走 ``json_schema``，
#: 而 DeepSeek 该 response_format 不可用（返回 400），function calling 才是通吃方案。
_STRUCTURED_METHOD: str = "function_calling"


class IntentClassifier(Protocol):
    """意图识别器协议。"""

    def classify(self, text: str) -> Intent:
        """识别单条用户输入的意图。"""

        ...


class IntentDecision(BaseModel):
    """意图判定的结构化输出契约。"""

    intent: Intent = Field(description="用户输入所属的意图分类")
    reason: str = Field(default="", description="判定理由，仅用于排查问题")


_SYSTEM_PROMPT = (
    "你是校园事务助手的前置意图识别模块。请判断用户输入属于以下哪一类：\n"
    "- chat：寒暄、闲聊、通用问答，与校园事务无关\n"
    "- knowledge：咨询校园规章制度、办事流程、政策说明等需要知识库支撑的问题\n"
    "- query：查询个人或教务数据，如成绩、课表、考试安排、学分\n"
    "- action：要求执行某项操作，如报名、申请、请假、提交材料\n"
    "- unknown：信息不足或不属于上述任何一类\n"
    "只做分类，不要回答用户的问题。"
)

_FALLBACK_PROMPT = _SYSTEM_PROMPT + "\n直接输出一个英文单词：" + " / ".join(INTENTS)


def normalize_intent(value: object) -> Intent:
    """把任意取值规整为合法意图，无法识别时返回 ``DEFAULT_INTENT``。"""

    if not isinstance(value, str):
        return DEFAULT_INTENT

    normalized = value.strip().strip("。.\"'` ").lower()
    if normalized in _INTENT_SET:
        return cast(Intent, normalized)

    for candidate in INTENTS:
        if candidate in normalized:
            return candidate
    return DEFAULT_INTENT


class LLMIntentClassifier:
    """基于大模型的意图识别器。

    主路径走结构化输出（function calling）；若当前供应商或模型不支持，
    自动降级为「只回答一个单词」的文本解析。降级是**一次性**的：
    一旦确认不可用即长期走文本路径，避免每轮都产生一次失败调用。
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm
        self._structured: Runnable[Any, Any] | None = None
        self._structured_available = True

    def classify(self, text: str) -> Intent:
        if not text.strip():
            return DEFAULT_INTENT

        if self._structured_available:
            intent = self._classify_structured(text)
            if intent is not None:
                return intent

        return self._classify_by_text(text)

    def _classify_structured(self, text: str) -> Intent | None:
        """结构化识别；失败时关闭该路径并返回 ``None`` 交由调用方降级。"""

        if self._structured is None:
            try:
                self._structured = self._llm.with_structured_output(
                    IntentDecision, method=_STRUCTURED_METHOD
                )
            except Exception:  # noqa: BLE001 - 供应商能力差异，降级而非中断
                logger.warning(
                    "模型无法绑定 %s 结构化输出，意图识别改用文本解析",
                    _STRUCTURED_METHOD,
                    exc_info=True,
                )
                self._structured_available = False
                return None

        try:
            decision = self._structured.invoke(
                [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=text)]
            )
        except Exception:  # noqa: BLE001 - 结构化路径运行期失败，永久降级
            logger.warning("结构化意图识别失败，后续改用文本解析", exc_info=True)
            self._structured_available = False
            return None

        return normalize_intent(getattr(decision, "intent", decision))

    def _classify_by_text(self, text: str) -> Intent:
        """降级路径：让模型直接吐出一个意图单词。"""

        try:
            response = self._llm.invoke(
                [SystemMessage(content=_FALLBACK_PROMPT), HumanMessage(content=text)]
            )
        except Exception:  # noqa: BLE001 - 模型/网络异常，交由调用方兜底
            logger.exception("意图识别调用失败")
            return DEFAULT_INTENT

        content = response.content
        return normalize_intent(content if isinstance(content, str) else str(content))


@lru_cache(maxsize=1)
def get_intent_classifier() -> IntentClassifier:
    """返回进程内共享的默认识别器（惰性构建，导入期不读取配置）。"""

    return LLMIntentClassifier(get_llm())
