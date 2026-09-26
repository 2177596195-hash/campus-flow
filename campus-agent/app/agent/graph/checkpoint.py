"""会话检查点存储器工厂。

职责边界：只负责「为图提供会话状态持久化实现」这一项基础设施，
不感知图拓扑、不感知节点实现、不持有任何会话数据本身。

已登记后端（登记表 ``_CHECKPOINTER_FACTORIES``：后端名 -> 工厂）：

- ``memory``  LangGraph 官方进程内实现 ``InMemorySaver``：零外部依赖、零配置，
  但状态**只活在当前进程内**——进程退出即丢失，换个进程拿同一个 ``thread_id``
  什么也取不回来。默认后端，也是单轮调用与测试用的后端。
- ``sqlite``  LangGraph 官方 ``SqliteSaver``（来自独立分发的
  ``langgraph-checkpoint-sqlite``）：状态落到本地 SQLite 文件，
  **进程重启后同一 ``thread_id`` 仍能取回状态**，因此多轮任务可以跨进程续上。
  数据库文件位置由 ``CHECKPOINTER_SQLITE_PATH`` 决定（默认落在
  ``campus-agent/.storage/checkpoints/campus_flow.sqlite``，不进版本库），
  装配细节见 ``app/agent/graph/checkpoint_sqlite.py``。

默认后端刻意保持 ``memory``：本阶段的目标是「让持久化成为**一个选项**」，
不是改变既有单轮 / REPL 行为的默认取值。

用法::

    checkpointer = create_checkpointer(
        settings.checkpointer_backend, sqlite_path=settings.checkpointer_sqlite_path
    )
    graph = build_graph(checkpointer=checkpointer)
    ...
    close_checkpointer(checkpointer)      # 入口收尾时释放后端资源
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agent.graph.checkpoint_sqlite import (
    build_sqlite_checkpointer,
    close_sqlite_checkpointer,
)

logger = logging.getLogger(__name__)

#: 未显式指定 CHECKPOINTER_BACKEND 时使用的后端
DEFAULT_BACKEND: Final[str] = "memory"

#: SQLite 后端名（``CHECKPOINTER_BACKEND=sqlite``）
SQLITE_BACKEND: Final[str] = "sqlite"


class CheckpointerError(RuntimeError):
    """检查点后端配置非法或不可用。"""


@dataclass(frozen=True, slots=True)
class CheckpointerOptions:
    """装配后端时可能用到的可选参数。

    做成一个值对象是为了让登记表里的工厂签名**统一**为
    ``Callable[[CheckpointerOptions], BaseCheckpointSaver]``：没有哪个后端需要
    「额外多传一个参数」的特例，因此 ``create_checkpointer`` 的调用方不必
    按后端名写不同的分支，新增后端也不必改调用方。后端忽略自己不关心的字段
    （``memory`` 就完全不用它）。
    """

    #: SQLite 后端使用的数据库文件路径；其余后端忽略。
    sqlite_path: Path | None = None


def _create_memory(_options: CheckpointerOptions) -> BaseCheckpointSaver:
    """进程内后端：不持有任何外部资源。"""

    return InMemorySaver()


def _create_sqlite(options: CheckpointerOptions) -> BaseCheckpointSaver:
    """文件后端：路径缺失在这里就报错，绝不悄悄落到某个默认位置。"""

    if options.sqlite_path is None:
        # 「配置从哪来」是登记表这一层的事，所以缺参的报错也留在这一层；
        # 「拿到路径之后怎么建」才是 checkpoint_sqlite 的事。
        raise CheckpointerError(
            f"CHECKPOINTER_BACKEND={SQLITE_BACKEND} 需要 SQLite 数据库文件路径，"
            "请配置 CHECKPOINTER_SQLITE_PATH（或调用时传入 sqlite_path）"
        )
    return build_sqlite_checkpointer(options.sqlite_path)


#: 后端名 -> 存储器工厂。接入新后端（PostgreSQL / Redis …）只需在此登记一行。
_CHECKPOINTER_FACTORIES: Final[
    dict[str, Callable[[CheckpointerOptions], BaseCheckpointSaver]]
] = {
    DEFAULT_BACKEND: _create_memory,
    SQLITE_BACKEND: _create_sqlite,
}


def supported_backends() -> tuple[str, ...]:
    """返回已登记的后端名列表。"""

    return tuple(sorted(_CHECKPOINTER_FACTORIES))


def create_checkpointer(
    backend: str | None = None, *, sqlite_path: Path | None = None
) -> BaseCheckpointSaver:
    """按后端名构建检查点存储器。

    Args:
        backend: 后端名；省略时使用 ``DEFAULT_BACKEND``。
        sqlite_path: SQLite 后端使用的数据库文件路径；其余后端忽略。
            正常入口从配置取（``AppSettings.checkpointer_sqlite_path``），
            测试则直接传临时目录，因此这里不做任何「猜默认路径」的兜底。

    Returns:
        可直接注入 ``build_graph(checkpointer=...)`` 的存储器实例。

    Raises:
        CheckpointerError: 后端名未登记，或该后端缺少必需参数。
    """

    name = (backend or DEFAULT_BACKEND).strip().lower()
    factory = _CHECKPOINTER_FACTORIES.get(name)
    if factory is None:
        supported = ", ".join(supported_backends())
        raise CheckpointerError(f"不支持的 CHECKPOINTER_BACKEND: {name!r}，可选值: {supported}")

    saver = factory(CheckpointerOptions(sqlite_path=sqlite_path))
    logger.info("会话检查点后端: %s (%s)", name, type(saver).__name__)
    return saver


def close_checkpointer(saver: BaseCheckpointSaver | None) -> None:
    """释放检查点后端占用的资源；无外部资源的实现是空操作。

    为什么需要它：``InMemorySaver`` 没有任何外部资源，进程退出即回收；
    ``SqliteSaver`` 则持有一条 sqlite3 连接，放着不管就是一条**永不关闭的连接**
    ——交互式 REPL 这类长驻入口尤其不该这样。LangGraph 给的生命周期原语是
    ``SqliteSaver.from_conn_string()`` 上下文管理器，而编译后的图必须在
    ``with`` 之外长期使用，因此这里提供等价的显式关闭动作，由入口在
    ``finally`` 里调用。

    后端各自知道自己要释放什么，所以判定用 ``isinstance`` 而不是
    「有没有 ``conn`` 属性」——避免把无关对象的偶然属性当成可关闭资源。
    新增持有资源的后端时，在此补一条分支即可（这是「怎么关」的知识，
    与登记表里「怎么建」对称）。
    """

    if saver is None:  # 无检查点（如未注入的模块级图）时调用是安全的
        return
    if isinstance(saver, SqliteSaver):
        close_sqlite_checkpointer(saver)
