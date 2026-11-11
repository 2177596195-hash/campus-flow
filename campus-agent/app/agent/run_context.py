"""一次 Agent 调用的 run 上下文（LangGraph 原生 config 的 run_name / tags / metadata）。

本模块只做一件事：把「一次用户请求属于哪个项目 / 哪个会话 / 哪个入口 / 哪类意图」
落成 **LangGraph / LangChain 原生的 config 字段**，让一次请求在追踪平台的 run 树上
一眼可读。它不引入任何新的追踪机制——不注册回调、不建客户端、不在节点里手写埋点，
业务结果一个字节都不受影响。

原生字段的实际行为（实测确认，是本模块全部设计的依据）
------------------------------------------------------
================  ==========================================================
字段               行为
================  ==========================================================
``run_name``      只作用于本次 invoke 的那个**根 run**
``tags``          随 config 传播到该次调用产生的**全部子 run**
``metadata``      同上；且 ``configurable.thread_id`` 会被 LangGraph
                  自动补一个 ``thread_id`` 键进 metadata
================  ==========================================================

意图为什么走 tag 而不是 metadata
--------------------------------
``intent`` 要等 ``intent_router`` 跑完才知道，而 config 必须在 invoke **之前**给出，
所以根 run 上**不可能**有 intent——除非在入口再识别一次意图，那会平白多一次模型
调用、改变业务行为，本阶段明确不做。

于是 intent 走 ``tags``：``intent_router`` 把自己算出的意图以 ``intent:<取值>``
追加进 ``config["tags"]``。这不是绕路，而是根据实测选择的唯一可靠通道：
LangGraph 在 super-step 之间**按引用**传递 tags 列表，而 metadata 每个节点拿到的是
一个新 dict（节点内的写入传不到下游 run）。因此「router 之后开始的 run」——分支节点、
模型调用、工具执行——会**一致地**带上意图标签，而标签恰好是追踪平台可筛选的维度。

代价是明确的：``intent_router`` 自己的 run 与根 run 不会带意图标签（标签在 run 开始
时即被记录）。要确认某次请求的意图，看根 run 之下任意一个子 run，或看
``intent_router`` / ``route_by_intent`` 的输出即可。

同理，``intent_router`` 内部为「识别意图」而发起的那几个 run（结构化输出的
``RunnableSequence`` / ``ChatOpenAI`` / ``PydanticToolsParser``）开始于意图算出来
**之前**，也不带意图标签——它们正是「判定意图」这件事本身。用静态识别器（测试）
时不会有这几个 run，所以离线用例里不带标签的 run 只有「根 run + intent_router」。
"""

from __future__ import annotations

from typing import Any, Final, Optional

from langchain_core.runnables import RunnableConfig

from app.agent.intent import INTENTS, normalize_intent

#: 根 run 名称：一次用户请求 == 一次 Agent 调用
RUN_NAME: Final[str] = "campus-flow.turn"

#: 项目标识。tag 与 metadata 各出现一份：前者用于筛选，后者用于展示
PROJECT_NAME: Final[str] = "campus-flow"

#: 运行入口 tag 前缀与取值（同一进程内多种入口：单次提问 / 交互模式 / HTTP）
ENTRYPOINT_TAG_PREFIX: Final[str] = "entry:"
ENTRYPOINT_CLI_SINGLE: Final[str] = "cli-single"
ENTRYPOINT_CLI_REPL: Final[str] = "cli-repl"
#: FastAPI HTTP 入口（app/api/server.py）：前端经 Spring Boot 转发过来的请求走这里
ENTRYPOINT_HTTP: Final[str] = "http"

#: 意图 tag 前缀。合法取值直接由意图契约派生，不再维护第二份清单
INTENT_TAG_PREFIX: Final[str] = "intent:"

#: 全部合法意图 tag（与 ``app.agent.intent.INTENTS`` 同源）
INTENT_TAGS: Final[tuple[str, ...]] = tuple(
    f"{INTENT_TAG_PREFIX}{intent}" for intent in INTENTS
)


def intent_tag(intent: object) -> str:
    """返回某个意图对应的 tag，非法取值一律规整为 ``unknown``。

    复用意图契约的 ``normalize_intent``：本模块不猜测、也不接受任意字符串，
    否则一个识别异常就会把脏数据灌进标签。
    """

    return f"{INTENT_TAG_PREFIX}{normalize_intent(intent)}"


def build_run_context(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    entrypoint: str | None = None,
) -> RunnableConfig:
    """构造一次 Agent 调用的运行上下文（原生 config 字段）。

    Args:
        session_id: 会话标识，同时作为 checkpointer 的 ``thread_id``。
        user_id: 用户标识；当前取自校园业务配置里的学号占位（接入登录态前）。
        entrypoint: 运行入口标识（如 ``cli-single`` / ``cli-repl``）。

    Returns:
        可直接作为 ``graph.invoke(..., config=...)`` 的原生 config。

    空值一律**不写入**：没有 session_id / user_id 时（直接调用图、测试、或尚未
    接入登录态的部署）不得因为缺字段报错，也不该在追踪里留下一串空字符串噪声。
    没有 ``session_id`` 时也就不写 ``configurable.thread_id``——checkpointer 需要它，
    但那属于调用方是否启用会话的语义，不该由本模块编造一个。
    """

    tags: list[str] = [PROJECT_NAME]
    if entrypoint:
        tags.append(f"{ENTRYPOINT_TAG_PREFIX}{entrypoint}")

    metadata: dict[str, Any] = {"project": PROJECT_NAME}
    if session_id:
        metadata["session_id"] = session_id
    if user_id:
        metadata["user_id"] = user_id
    if entrypoint:
        metadata["entrypoint"] = entrypoint

    config: RunnableConfig = {
        "run_name": RUN_NAME,
        "tags": tags,
        "metadata": metadata,
    }
    if session_id:
        config["configurable"] = {"thread_id": session_id}
    return config


def annotate_intent(
    config: Optional[RunnableConfig], intent: object
) -> str:
    """把意图追加为 run tag（供 router 之后开始的 run 继承），返回写入的 tag。

    为什么这件事只能由 ``intent_router`` 做：它是唯一同时握着「刚算出的意图」与
    「当前 config」的地方。这里**不写状态、不改路由、不重跑识别**，只往 config 的
    tags 里追加一个字符串，因此对业务行为零影响。

    写入是**幂等**的：标签已存在就不重复追加（节点在同一 super-step 内被重放时
    不会把标签堆起来）。``tags`` 缺失或不是列表时按现有取值补一个列表——
    追加的仍是调用方传进来/上游传下来的那个对象，下游 run 才看得到。
    """

    tag = intent_tag(intent)
    if not isinstance(config, dict):
        return tag

    existing = config.get("tags")
    if isinstance(existing, list):
        target = existing
    else:
        target = list(existing or ())
        config["tags"] = target

    if tag not in target:
        target.append(tag)
    return tag


__all__ = [
    "ENTRYPOINT_CLI_REPL",
    "ENTRYPOINT_CLI_SINGLE",
    "ENTRYPOINT_HTTP",
    "ENTRYPOINT_TAG_PREFIX",
    "INTENT_TAGS",
    "INTENT_TAG_PREFIX",
    "PROJECT_NAME",
    "RUN_NAME",
    "annotate_intent",
    "build_run_context",
    "intent_tag",
]
