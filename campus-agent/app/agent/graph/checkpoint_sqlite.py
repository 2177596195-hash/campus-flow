"""SQLite 检查点后端（LangGraph 官方同步实现）的装配细节。

职责边界：本文件只回答一件事——**怎样把一个 SQLite 文件变成可注入图的
检查点存储器**。不感知图拓扑、不感知会话内容、不读配置（文件路径一律由
调用方经配置传入），与 ``checkpoint.py`` 的分工是：

    checkpoint.py          后端名 -> 工厂（登记表；新增后端只多一行）
    checkpoint_sqlite.py   其中一个后端的具体装配（目录怎么保、连接怎么建/怎么关）

为什么单独一个文件而不是全写进登记表：**「有哪些后端」与「某个后端怎么装配」
是两件会各自变化的事**。SQLite 的装配细节（连接参数、目录自动创建、生命周期）
不该挤进登记表；日后接入 PostgreSQL / Redis 时同理各写一个文件，
登记表不会长成 if/else。

生命周期：LangGraph 的 ``SqliteSaver`` **自身不提供 ``close()``**，官方给的
生命周期原语是 ``SqliteSaver.from_conn_string()`` 这个上下文管理器（退出时
用 ``contextlib.closing`` 关掉底层连接）。但图一旦装配完成就会在 ``with``
之外被反复调用，因此本模块把「建连接」与「关连接」拆成两个显式动作，
由 ``app/agent/graph/checkpoint.py::close_checkpointer()`` 在入口收口，
避免出现一条永不关闭的连接。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

logger = logging.getLogger(__name__)


def build_sqlite_checkpointer(path: Path) -> BaseCheckpointSaver:
    """构建以 ``path`` 为数据库文件的 SQLite 检查点存储器。

    三件事，各自都有理由：

    - **父目录自动创建**：默认位置是 ``campus-agent/.storage/checkpoints/``，
      这个目录和向量库一样属于「可由程序重建的本地数据」，全新克隆的仓库里
      并不存在，不该要求使用者先手工建目录。
    - **``check_same_thread=False``**：LangGraph 允许同一存储器被不同线程使用
      （本项目连 MCP Server 的客户端就在后台线程里跑），而 ``sqlite3`` 默认
      禁止跨线程使用连接。官方 ``from_conn_string`` 用的也是这个取值。
    - **建表交给官方实现**：``SqliteSaver.setup()`` 是惰性的（首次读写时
      自动执行 ``CREATE TABLE IF NOT EXISTS ...``），官方文档明确它
      "should not be called directly by the user"，因此这里不代劳。

    Args:
        path: SQLite 数据库文件路径（``.sqlite`` 后缀只是习惯，不是要求）。

    Returns:
        可注入 ``build_graph(checkpointer=...)`` 的存储器实例。
    """

    _ensure_parent_dir(path)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(connection)
    logger.info("会话检查点文件: %s", path)
    return saver


def close_sqlite_checkpointer(saver: SqliteSaver) -> None:
    """关闭 ``SqliteSaver`` 持有的 sqlite3 连接。

    与官方 ``from_conn_string`` 退出时做的事完全一致（那边也是直接
    ``conn.close()``），只是把动作从「缩进块结束」改成「入口显式调用」——
    因为编译好的图要在 ``with`` 之外长期复用。
    """

    saver.conn.close()
    logger.debug("已关闭 SQLite 检查点连接")


def _ensure_parent_dir(path: Path) -> None:
    """确保数据库文件的父目录存在（已存在则什么都不做）。"""

    parent = path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        logger.debug("已创建检查点目录: %s", parent)


__all__ = [
    "build_sqlite_checkpointer",
    "close_sqlite_checkpointer",
]
