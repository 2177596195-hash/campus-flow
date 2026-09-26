"""Answer Relevance 评测模型（LLM-as-a-Judge，只做这一个指标）。

职责边界：只回答一个问题——**这段回答是否针对用户提出的问题**。
判定输入只有两项：**用户问题 + Agent 最终回答**。

刻意**不把检索到的文档当作判定依据**：那是 Faithfulness / Citation 一类指标的输入，
本模块不做任何事实性判断，因此也不 import 任何检索 / 向量库相关模块
（评估层的「不越过 Retriever」约束对本文件同样成立）。

结构化输出沿用项目既有手法（``with_structured_output(..., method="function_calling")``，
与 ``app.agent.intent`` 完全一致——DeepSeek 不接受 ``json_schema`` 响应格式）：

    AnswerRelevanceVerdict -> {"score": 0.0~1.0, "reason": "<判定理由>"}

**失败绝不向上抛**：模型不可用 / 调用失败 / 分数越界或非数字，一律返回
``AnswerRelevanceJudgement(score=None, reason=<可读原因>)``。
把「判不出来」记成 ``None`` 而不是 0 分，是因为 0 分表示「确实答非所问」，
两者混在一起会把基础设施故障伪装成回答质量问题。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: 允许的分数区间（闭区间，两端都合法）
SCORE_MIN: Final[float] = 0.0
SCORE_MAX: Final[float] = 1.0

#: 评测用温度。判定要可复现，因此固定为 0——这是**唯一**为 Judge 做的模型参数调整，
#: provider / model / api_key / base_url / timeout 全部复用既有 LLM 配置，没有第二套配置。
JUDGE_TEMPERATURE: Final[float] = 0.0

#: 结构化输出方式。必须显式指定：ChatOpenAI 默认走 ``json_schema``，
#: 而 DeepSeek 该 response_format 不可用（返回 400），function calling 才是通吃方案。
_STRUCTURED_METHOD: Final[str] = "function_calling"

_SYSTEM_PROMPT: str = (
    "你是校园事务助手的回答质量评测员。只判断一件事：**助手的回答是否针对用户提出的问题**。\n"
    "评分标准（只按「是否回答了这个问题」打分）：\n"
    "- 1.0：完全针对问题，直接回答核心需求\n"
    "- 0.7~0.9：基本回答了问题，但存在少量无关内容或遗漏\n"
    "- 0.4~0.6：只部分回答了问题\n"
    "- 0.1~0.3：大部分没有回答问题\n"
    "- 0.0：完全没有回答问题 / 答非所问\n"
    "必须遵守的规则：\n"
    "1. 这是**回答相关性**判断，不是事实正确性判断。不要因为回答里的内容可能有误就给低分；\n"
    "   只要它确实围绕用户的问题作出了回应，就按上面的档位给分。\n"
    "2. 回答若明确说明「知识库中没有相关资料」，但仍在回应用户所问的方向，"
    "不要直接判 0 分——按它是否切题给分。\n"
    "3. 不要臆测用户没问过的需求，也不要把回答的长度当质量。\n"
    "4. 在 reason 里用一句中文说明打分依据（引用回答中的关键表述，不要复述整段回答）。"
)

_USER_TEMPLATE: str = (
    "【用户问题】\n{question}\n\n"
    "【助手回答】\n{answer}\n\n"
    "请给出 score（0.0~1.0）与 reason。"
)


class AnswerRelevanceVerdict(BaseModel):
    """评测模型的结构化输出契约。

    ``score`` 刻意**不加** pydantic 区间约束：越界分数要在下面被显式识别并记成
    「无法判定」，而不是变成一个 ValidationError 文案——那样 comment 会看不出
    究竟是模型不听话还是链路坏了。
    """

    score: float = Field(description="回答与问题的相关性分数，取值 0.0~1.0")
    reason: str = Field(default="", description="一句中文说明打分依据")


@dataclass(frozen=True, slots=True)
class AnswerRelevanceJudgement:
    """一次评测的结果：要么给出分数，要么给出无法判定的原因。"""

    #: ``None`` 表示无法判定（模型失败 / 分数非法），**不是 0 分**
    score: float | None = None
    reason: str = ""


class AnswerJudge(Protocol):
    """评测模型协议：评估适配层只依赖它，因此离线替身可直接顶替。"""

    def judge(self, question: str, answer: str) -> AnswerRelevanceJudgement:
        """判定「回答是否针对问题」。"""

        ...


def _field(value: Any, name: str) -> Any:
    """从结构化输出里读字段（容忍 pydantic 对象与 dict 两种形态）。"""

    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _merge(problem: str, reason: str) -> str:
    """把判定失败的原因与模型给的理由合成一句可读文本。"""

    return f"{problem}｜模型理由: {reason}" if reason else problem


def to_judgement(value: Any) -> AnswerRelevanceJudgement:
    """把结构化输出规整为判定结果；分数非法时返回「无法判定」而不是猜测。"""

    reason_value = _field(value, "reason")
    reason = reason_value.strip() if isinstance(reason_value, str) else ""

    raw_score = _field(value, "score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        return AnswerRelevanceJudgement(reason=_merge("评测模型未返回合法分数", reason))

    score = float(raw_score)
    if not math.isfinite(score) or not SCORE_MIN <= score <= SCORE_MAX:
        return AnswerRelevanceJudgement(
            reason=_merge(f"评测模型返回越界分数 {score}", reason)
        )

    return AnswerRelevanceJudgement(score=score, reason=reason or "评测模型未给出理由")


class AnswerRelevanceJudge:
    """基于大模型的回答相关性评测器。

    结构化输出**只绑定一次**：若模型/供应商不支持 ``function_calling``，
    后续条目直接返回「无法判定」，不会每行都产生一次注定失败的调用。
    运行期单次调用失败（网络 / 限流）只影响该条目——不永久降级，
    因为那是暂时性故障，不应污染其余条目的判定。
    """

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm
        self._structured: Runnable[Any, Any] | None = None
        self._unavailable: str | None = None

    def judge(self, question: str, answer: str) -> AnswerRelevanceJudgement:
        if not question.strip():
            return AnswerRelevanceJudgement(reason="缺少用户问题")
        if not answer.strip():
            return AnswerRelevanceJudgement(reason="缺少最终回答")

        runnable = self._resolve_structured()
        if runnable is None:
            assert self._unavailable is not None  # 由 _resolve_structured 保证
            return AnswerRelevanceJudgement(reason=self._unavailable)

        try:
            verdict = runnable.invoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(
                        content=_USER_TEMPLATE.format(
                            question=question.strip(), answer=answer.strip()
                        )
                    ),
                ]
            )
        except Exception:  # noqa: BLE001 - 单条判定失败不向上抛，也不影响其它条目
            logger.warning("回答相关性评测调用失败", exc_info=True)
            return AnswerRelevanceJudgement(reason="评测模型调用失败")

        return to_judgement(verdict)

    def _resolve_structured(self) -> Runnable[Any, Any] | None:
        """惰性绑定结构化输出；绑定失败即永久标记为不可用。"""

        if self._structured is not None:
            return self._structured
        if self._unavailable is not None:
            return None

        try:
            self._structured = self._llm.with_structured_output(
                AnswerRelevanceVerdict, method=_STRUCTURED_METHOD
            )
        except Exception:  # noqa: BLE001 - 供应商能力差异，降级而非中断
            logger.warning(
                "模型无法绑定 %s 结构化输出，回答相关性评测将无法判定",
                _STRUCTURED_METHOD,
                exc_info=True,
            )
            self._unavailable = f"评测模型不支持 {_STRUCTURED_METHOD} 结构化输出"
            return None
        return self._structured


__all__ = [
    "JUDGE_TEMPERATURE",
    "SCORE_MAX",
    "SCORE_MIN",
    "AnswerJudge",
    "AnswerRelevanceJudge",
    "AnswerRelevanceJudgement",
    "AnswerRelevanceVerdict",
    "to_judgement",
]
