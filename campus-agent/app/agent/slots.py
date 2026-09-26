"""任务槽位（slot）语义与槽位解析：多轮任务状态的最小支撑。

本模块只回答三件事，而且都是**纯函数**、确定性、可离线断言：

1. 「这句话说的是哪个学期」——把用户口语（``本学期`` / ``上学期`` /
   ``2024-2025-2``）落成一个具体学期串；
2. 「这轮请求缺哪个槽位」——当前登记了两个场景：**成绩查询没有指定学期**、
   **请假申请没有给出起止时间与事由**；
3. 「上一轮补齐的槽位值该怎么交给模型」——把值渲染成一句任务上下文。

为什么不写「更聪明」的自然语言理解
----------------------------------

本阶段要的是**基础**多轮任务状态，因此这里刻意只覆盖演示场景需要的最小集合：
不引入第二个模型调用、不引入新配置项、不猜意图。判定完全确定，因此可以被
逐条单测；需要更多槽位时按同一形状扩展（登记表 + 纯函数），
而不是把它长成一个通用 NLU 层。

四个刻意的取舍
--------------

- **成绩触发词只收「成绩」「分数」两个词**，不收「分」「考」这类高频单字。
  「数据结构考了多少分」是**已经带了课程范围**的请求，本身可以作答，
  不该被拦下来追问学期；只有「帮我查一下成绩」这种完全没有范围的请求
  才需要补学期。
- **多域请求不追问**：一句话里同时提到课表 / 考试 / 教师时，不问学期。
  拦下这种请求会把用户已经说出口的其它几项丢掉，比少追问更糟。
- **请假只在「信息明显不全」时才拦**：请假要三个字段（起止时间 + 事由），
  已经说全的请求不该被拦下来重问一遍。判据是确定性的启发式
  （见 :func:`_leave_detail_present`），并且**宁严不宽**——
  判「不全」只是多问一句（无副作用），判「全了」却可能让模型在没有时间的情况下
  去提交申请。因此这条规则刻意偏保守。
- **``pending_value`` 自描述**：请假槽位的值带一个显式的 ``槽位名:`` 前缀
  （见 :func:`encode_leave_detail`），学期槽位仍是裸字符串。这样
  :func:`slot_hint` 不需要额外的状态字段或「猜格式」就能区分两者，
  既有的单槽位路径也**一个字节都没变**。
  没有升级成 ``{slot: value}`` 的字典结构：当前只有两个槽位，且各自的值形态
  互斥，用前缀是更小的改动（见完成报告的「多槽位最小实现」）。

本模块不依赖 LangChain / LangGraph / 模型 / 网络，也不读环境变量，
因此既能被节点在运行期调用，也能被离线用例直接断言。
"""

from __future__ import annotations

import logging
import re
from typing import Final

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 任务与槽位

#: 需要「学期」这一槽位的意图取值。
#: 用字面量而不是 ``from app.agent.intent import Intent``，是为了让本模块保持
#: 零依赖（``intent`` 模块会顺带拉进 LangChain 模型层）。
#: 一致性由测试守卫：该字面量必须仍在 ``app.agent.intent.INTENTS`` 里。
SCORE_QUERY_INTENT: Final[str] = "query"

#: 需要「请假补充信息」这一槽位的意图取值。
#: 与上面同理：请假属于「要求执行某项操作」，意图契约里就是 ``action``。
LEAVE_ACTION_INTENT: Final[str] = "action"

#: 槽位名：学期。
SLOT_SEMESTER: Final[str] = "semester"

#: 槽位名：请假申请的补充信息（起止时间 + 事由）。
SLOT_LEAVE: Final[str] = "leave_detail"

#: 本阶段已登记的槽位（顺序即登记顺序）。
TASK_SLOT_NAMES: Final[tuple[str, ...]] = (SLOT_SEMESTER, SLOT_LEAVE)

#: 请假槽位值的显式前缀（形态即 ``槽位名:值``）。见模块 docstring 的取舍说明。
_LEAVE_VALUE_PREFIX: Final[str] = f"{SLOT_LEAVE}:"

# ------------------------------------------------------------------ 学期解析

#: 显式学期串：``2025-2026-1``。
#: 前后用 ``(?<!\d)`` / ``(?!\d)`` 卡住，避免把 ``2025-2026-12`` 之类的串
#: 截出一个「看着合法」的学期——宁可判不出，也不要把错值当成用户本意。
SEMESTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{4})-(\d{4})-([12])(?!\d)")

#: 「本学期」这类口语
CURRENT_SEMESTER_KEYWORDS: Final[tuple[str, ...]] = (
    "本学期",
    "这个学期",
    "这学期",
    "当前学期",
)

#: 「上学期」这类口语（指当前学期的上一个学期）
PREVIOUS_SEMESTER_KEYWORDS: Final[tuple[str, ...]] = (
    "上学期",
    "上一学期",
    "上个学期",
    "前一个学期",
)

#: 「这是成绩相关的问题」的触发词。见模块 docstring 里的取舍说明。
SCORE_KEYWORDS: Final[tuple[str, ...]] = ("成绩", "分数")

#: 同一句话里出现这些词，说明用户要的**不止**成绩（课表 / 考试 / 教师 …）。
#:
#: 为什么因此就不追问：把一句多域请求拦下来问「哪个学期」，会丢掉用户已经说出口的
#: 另外几项（「课表、考试、成绩都给我」很常见），比不追问更糟。本阶段只在
#: 「整句话就是在要成绩」时才补槽位——这是**有意**的窄口径，宁可少追问，
#: 也不要打断用户本来就完整的请求。
OTHER_DOMAIN_KEYWORDS: Final[tuple[str, ...]] = (
    "课表",
    "课程",
    "上课",
    "考试",
    "考场",
    "教室",
    "教师",
    "老师",
    "星期",
    "周几",
    "第几周",
)

# ------------------------------------------------------------------ 请假识别

#: 「这是请假申请」的触发词。「请假」是「请假条」「提交请假」等说法的子串，
#: 因此只需补上不含它的口语变体「请个假」。
LEAVE_KEYWORDS: Final[tuple[str, ...]] = ("请假", "请个假")

#: 日期 / 时刻的样子。只用于「信息看起来给全了吗」这一个判断，不参与解析——
#: 真正的字段提取交给模型（见 :func:`slot_hint` 的请假分支）。
#: 覆盖 ``2026-10-12`` / ``10月12日`` / ``08:00`` 三类常见写法。
_DATE_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?:\d{4}[-/])?\d{1,2}[-/月]\d{1,2}(?:[日号])?(?!\d)"
    r"|(?<!\d)\d{1,2}[:：]\d{2}(?!\d)"
)

#: 区间连接词。有它时，一个日期即可视为「说了一个时间范围」。
_RANGE_MARKERS: Final[tuple[str, ...]] = ("到", "至", "~", "～", "—", "-", "－")

#: 判断「事由是否已经说了」时要剔除的填充词。
#: 实际剥离时按**长度降序**处理（见 :func:`_leave_reason_present`），
#: 因此这里的书写顺序不影响结果。
_LEAVE_FILLERS: Final[tuple[str, ...]] = (
    "麻烦帮我",
    "请假条",
    "请个假",
    "我要请",
    "麻烦",
    "帮我",
    "我要",
    "我想",
    "申请",
    "提交",
    "请假",
    "一下",
    "一个",
    "请",
)

#: 切分句子用的标点（事由通常单独成段）。
_CLAUSE_SEPARATORS: Final[tuple[str, ...]] = (
    "，", ",", "。", ".", "；", ";", "、", "！", "!", "？", "?", "\n",
)

#: 槽位名 -> 追问文案
_SLOT_QUESTIONS: Final[dict[str, str]] = {
    SLOT_SEMESTER: (
        "请问你要查询哪个学期的成绩？可以直接说「本学期」或「上学期」，"
        "也可以给出形如 2025-2026-1 的具体学期。"
    ),
    SLOT_LEAVE: (
        "请补充请假的起止时间和事由，例如「2026-10-12 08:00 到 2026-10-14 18:00，感冒发烧」。"
    ),
}


def current_semester() -> str:
    """当前学期（教务日历上的位置）。

    唯一来源是配置层 ``app.config.settings.get_campus_calendar_settings()``
    （``CAMPUS_CURRENT_SEMESTER`` 环境变量可覆盖）：本模块只做
    「口语 -> 学期串」的映射，不维护第二份日历常量。惰性导入是为了让本模块
    在被单独导入（含离线单测）时不触发配置读取，保持纯函数模块的性质。
    """

    from app.config.settings import get_campus_calendar_settings

    return get_campus_calendar_settings().current_semester


def _semester_parts(value: str) -> tuple[int, int, int] | None:
    """拆出 ``(起始学年, 结束学年, 学期序号)``；格式不合法返回 ``None``。"""

    match = SEMESTER_PATTERN.fullmatch((value or "").strip())
    if match is None:
        return None
    start, end, term = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if end != start + 1:
        return None
    return start, end, term


def parse_semester(text: str) -> str | None:
    """从文本里抽出**显式**学期串（形如 ``2025-2026-1``）。

    判不出或格式不自洽（如 ``2025-2030-1``）时返回 ``None``——
    「没听出来」比「猜一个」安全，调用方据此可以继续追问。
    """

    match = SEMESTER_PATTERN.search(text or "")
    if match is None:
        return None
    candidate = match.group(0)
    return candidate if _semester_parts(candidate) is not None else None


def previous_semester(semester: str) -> str | None:
    """返回某个学期的**上一个**学期；格式不合法返回 ``None``。

    学年内部的换季只是学期序号减一（``2025-2026-2`` -> ``2025-2026-1``）；
    跨学年时才需要把两个学年同时前移（``2025-2026-1`` -> ``2024-2025-2``）。
    """

    parts = _semester_parts(semester)
    if parts is None:
        return None
    start, end, term = parts
    if term == 2:
        return f"{start}-{end}-1"
    return f"{start - 1}-{end - 1}-2"


def mentions_semester(text: str) -> bool:
    """文本里是否**已经**给出了学期范围（显式串或口语说法）。"""

    if parse_semester(text) is not None:
        return True
    normalized = text or ""
    return any(
        token in normalized
        for token in (*CURRENT_SEMESTER_KEYWORDS, *PREVIOUS_SEMESTER_KEYWORDS)
    )


def mentions_score(text: str) -> bool:
    """文本是否在问成绩 / 分数。"""

    normalized = text or ""
    return any(token in normalized for token in SCORE_KEYWORDS)


def mentions_other_domain(text: str) -> bool:
    """文本是否还牵涉成绩以外的教务数据（课表 / 考试 / 教师 / 周次 …）。"""

    normalized = text or ""
    return any(token in normalized for token in OTHER_DOMAIN_KEYWORDS)


def mentions_leave(text: str) -> bool:
    """文本是否在提请假。"""

    normalized = text or ""
    return any(token in normalized for token in LEAVE_KEYWORDS)


def _leave_time_present(text: str) -> bool:
    """文本里是否已经给出了「起止时间」的样子。

    判据：至少两个日期 / 时刻标记，或一个标记加一个区间连接词。
    """

    normalized = text or ""
    count = sum(1 for _ in _DATE_TOKEN.finditer(normalized))
    if count >= 2:
        return True
    return count >= 1 and any(marker in normalized for marker in _RANGE_MARKERS)


def _leave_reason_present(text: str) -> bool:
    """文本里是否已经给出了「事由」的样子。

    判据：存在一个**不含任何时间标记**的分句，且剥掉请假相关的填充词之后
    仍有至少两个字。分句切分是必要的——「我要请假，10月12日到10月14日」
    整句看当然有字，但那两段分别是意图声明与时间，没有一个是事由。
    """

    normalized = text or ""
    for clause in re.split("|".join(re.escape(char) for char in _CLAUSE_SEPARATORS), normalized):
        if _DATE_TOKEN.search(clause):
            continue
        remainder = clause
        for filler in sorted(_LEAVE_FILLERS, key=len, reverse=True):
            remainder = remainder.replace(filler, "")
        if len(remainder.strip()) >= 2:
            return True
    return False


def _leave_detail_present(text: str) -> bool:
    """请假请求是否「看起来已经说全了」（起止时间与事由都有）。

    这是一条**刻意偏保守**的启发式：判「不全」的代价只是多问一句，
    判「全了」却可能让模型在信息不足时就去提交申请。因此宁可多问。
    """

    return _leave_time_present(text) and _leave_reason_present(text)


def encode_leave_detail(text: str) -> str | None:
    """把请假的补充信息编码成 ``leave_detail:`` 前缀的自描述值。

    为什么不做字段解析：起止时间是**自由文本**（「下周三到周五」「10月12日 8:00 起」），
    确定性地解析它需要一整套自然语言日期规则，且解析错误会直接变成一条错申请。
    因此这里只原样记录用户说了什么，字段提取交给模型（它在 ``tool_calls`` 里
    给出结构化参数，由 ``args_schema`` 把住类型）。这与学期槽位不同——
    学期串可以无损归一化，所以那条路径仍然做确定性解析。

    没有可记录的内容（``None`` / 纯空白）时返回 ``None``，调用方据此继续追问。
    """

    cleaned = (text or "").strip()
    if not cleaned:
        return None
    return f"{_LEAVE_VALUE_PREFIX}{cleaned}"


def decode_leave_detail(value: object) -> str | None:
    """从 ``pending_value`` 里取出请假补充信息；不是请假值则返回 ``None``。"""

    if not isinstance(value, str) or not value.startswith(_LEAVE_VALUE_PREFIX):
        return None
    payload = value[len(_LEAVE_VALUE_PREFIX) :].strip()
    return payload or None


def resolve_semester(text: str, *, current: str | None = None) -> str | None:
    """把用户口述的学期解析成具体学期串；解析不出返回 ``None``。

    只支持三类输入（与需求一致，不多做）：

    - 显式学期串：``2024-2025-2``；
    - 「上学期」系列 -> 当前学期的上一个学期；
    - 「本学期」系列 -> 当前学期。

    Args:
        text: 用户本轮输入。
        current: 当前学期；省略时取 :func:`current_semester`。
            显式传入是为了让用例不依赖真实教务日历。
    """

    explicit = parse_semester(text)
    if explicit is not None:
        return explicit

    normalized = text or ""
    if any(token in normalized for token in PREVIOUS_SEMESTER_KEYWORDS):
        return previous_semester(current or current_semester())
    if any(token in normalized for token in CURRENT_SEMESTER_KEYWORDS):
        return current or current_semester()
    return None


def missing_slot(text: str, intent: object) -> str | None:
    """判断本轮请求是否「需要某个槽位但没给」，返回缺失的槽位名。

    本阶段有两条规则：

    - ``query`` 意图下，**整句话就是在要成绩**、却没说学期 -> 缺
      :data:`SLOT_SEMESTER`；
    - ``action`` 意图下，在提请假、但起止时间或事由还没说全 -> 缺
      :data:`SLOT_LEAVE`。

    除此之外一律返回 ``None``（不拦截），因此所有既有问法都保持原样。
    """

    if intent == SCORE_QUERY_INTENT:
        if not mentions_score(text):
            return None
        if mentions_semester(text):
            return None
        if mentions_other_domain(text):
            return None
        return SLOT_SEMESTER

    if intent == LEAVE_ACTION_INTENT:
        if not mentions_leave(text):
            return None
        if _leave_detail_present(text):
            return None
        return SLOT_LEAVE

    return None


def slot_question(slot: str) -> str:
    """取某个槽位的追问文案（未登记时给一句兜底）。"""

    return _SLOT_QUESTIONS.get(slot, f"请补充「{slot}」后再查询。")


def slot_hint(value: str | None) -> str | None:
    """把已补齐的槽位值渲染成给模型的一句任务上下文。

    没有待使用的值（``None`` / 空串）时返回 ``None``，调用方据此**完全不改动**
    发给模型的消息——因此正常单轮链路一个字节都不会变。

    值的归属由**值自身**判定：带 ``leave_detail:`` 前缀的是请假补充信息，
    其余按学期处理。两句提示都只做一件事：把「已经确定的事实」告诉模型，
    让它据此调用工具。真正的取数 / 提交仍由 ``agent`` 节点产出的 ``tool_calls``
    交给 ``ToolNode`` 执行，本模块既不执行工具也不改写工具参数。
    """

    if not value:
        return None

    detail = decode_leave_detail(value)
    if detail is not None:
        return (
            "【任务上下文】用户此前要提交请假申请，本轮补充的信息是："
            f"{detail}\n"
            "请从中提取请假的开始时间、结束时间与事由，并调用 submit_leave_application "
            "提交（start_time / end_time 形如 2026-10-12 08:00，reason 为事由）；"
            "信息仍不完整时先向用户确认，不要凭空编造时间或事由。"
        )

    return (
        "【任务上下文】用户此前要查询成绩但未说明学期，本轮已确认学期为 "
        f"{value}。请直接调用 get_score 工具并把 semester 传成 \"{value}\"，"
        "用工具返回的真实数据作答；不要再向用户追问学期。"
    )


__all__ = [
    "CURRENT_SEMESTER_KEYWORDS",
    "LEAVE_ACTION_INTENT",
    "LEAVE_KEYWORDS",
    "OTHER_DOMAIN_KEYWORDS",
    "PREVIOUS_SEMESTER_KEYWORDS",
    "SCORE_KEYWORDS",
    "SCORE_QUERY_INTENT",
    "SEMESTER_PATTERN",
    "SLOT_LEAVE",
    "SLOT_SEMESTER",
    "TASK_SLOT_NAMES",
    "current_semester",
    "decode_leave_detail",
    "encode_leave_detail",
    "mentions_leave",
    "mentions_other_domain",
    "mentions_score",
    "mentions_semester",
    "missing_slot",
    "parse_semester",
    "previous_semester",
    "resolve_semester",
    "slot_hint",
    "slot_question",
]
