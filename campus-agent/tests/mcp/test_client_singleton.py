"""MCP 客户端进程内单例（``get_mcp_client`` / ``reset_mcp_client``）的回归测试。

背景（P3 审计发现）：``get_mcp_client()`` 曾缺少 ``@lru_cache``，导致

1. docstring 宣称的「进程内共享」不成立——每次调用都新建 ``StdioMCPClient``，
   若装配工具清单时多次取客户端，就会各自拉起一条 stdio 会话；
2. ``reset_mcp_client()`` 内部调用 ``get_mcp_client.cache_info()`` /
   ``.cache_clear()``，而函数上没有缓存装饰器，一调用就 ``AttributeError``。
   全仓库（含测试）此前无人调用它，所以 954 条用例全绿也没暴露。

修复即 ``@lru_cache(maxsize=1)``（与 ``get_campus_api_client`` 同一策略），
本文件把它钉死为可执行契约。

测试全部离线：配置用 ``make_mcp_settings()`` 替身注入（不读环境变量），
``StdioMCPClient`` 构造本身零副作用（不拉子进程——建连发生在 ``start()``），
因此这里**不需要**真实 MCP Server，更不需要 Spring Boot。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config.settings import ConfigurationError
from app.mcp import client as client_module
from app.mcp.client import StdioMCPClient, get_mcp_client, reset_mcp_client
from tests._fakes import make_mcp_settings


@pytest.fixture
def isolated_singleton(monkeypatch: pytest.MonkeyPatch):
    """给单例一个受控的配置来源，并保证进入 / 离开时缓存都是干净的。

    - 配置用替身注入：不依赖本机环境变量，测试可完全离线；
    - 夹具先 ``reset`` 一次：避免同进程其他用例留下的缓存实例串场；
    - 结束时再 ``reset`` 一次：把夹具期间构造的客户端关掉并清空缓存，
      不把「替身配置装配出的客户端」泄漏给同进程的后续用例。
    """

    monkeypatch.setattr(client_module, "get_mcp_settings", lambda: make_mcp_settings())
    reset_mcp_client()
    yield
    reset_mcp_client()


def test_get_mcp_client_returns_the_same_instance(isolated_singleton) -> None:
    """连续两次调用返回同一个实例——「进程内共享」从此是可断言的事实。"""

    first = get_mcp_client()
    second = get_mcp_client()

    assert first is second
    assert isinstance(first, StdioMCPClient)


def test_reset_then_get_returns_a_fresh_instance(isolated_singleton) -> None:
    """reset 之后再次获取：新实例，与旧实例不是同一个对象。"""

    old = get_mcp_client()
    reset_mcp_client()
    new = get_mcp_client()

    assert new is not old
    assert isinstance(new, StdioMCPClient)


def test_reset_mcp_client_never_raises_attribute_error(isolated_singleton) -> None:
    """reset 在「有缓存」与「无缓存」两个分支下都不抛 ``AttributeError``。

    修复前 ``get_mcp_client`` 没有 ``cache_info`` / ``cache_clear`` 属性，
    这两行会直接 ``AttributeError``；这里两条路径各调用一次作为回归锚点。
    """

    # 分支一：缓存为空（currsize == 0）
    reset_mcp_client()

    # 分支二：缓存非空（currsize > 0，走 close 旧实例的路径）
    get_mcp_client()
    reset_mcp_client()


def test_reset_closes_the_cached_client(isolated_singleton) -> None:
    """reset 会先关闭旧实例再清缓存——符合「换配置重建前先释放资源」的设计。"""

    cached = get_mcp_client()

    close_calls: list[StdioMCPClient] = []
    original_close = cached.close

    def _recording_close() -> None:
        close_calls.append(cached)
        original_close()

    cached.close = _recording_close  # type: ignore[method-assign]

    reset_mcp_client()

    assert close_calls == [cached], "reset 必须对缓存的旧实例恰好调用一次 close"
    # 关闭动作作用于旧实例：未启动过的客户端 close 是安全空操作
    #（不抛异常、不碰任何已释放资源），这里顺带验证它仍然安全。
    assert cached._thread is None and cached._session is None


def test_reset_with_empty_cache_is_a_safe_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存为空时 reset 不构造、不关闭任何客户端。"""

    monkeypatch.setattr(client_module, "get_mcp_settings", lambda: make_mcp_settings())
    reset_mcp_client()  # 确保起点为空

    constructed: list[Any] = []
    original_init = StdioMCPClient.__init__

    def _recording_init(self: StdioMCPClient, *args: Any, **kwargs: Any) -> None:
        constructed.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(StdioMCPClient, "__init__", _recording_init)

    try:
        reset_mcp_client()
    finally:
        monkeypatch.setattr(StdioMCPClient, "__init__", original_init)
        reset_mcp_client()  # 还原缓存状态

    assert constructed == [], "空缓存时 reset 不应装配新客户端"


def test_configuration_error_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置非法不会被缓存：修正配置后的下一次调用能重新装配成功。

    ``lru_cache`` 不缓存异常，因此「第一次配置坏了」不会把单例钉死在失败上。
    """

    calls: list[int] = []

    def _settings() -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ConfigurationError("CAMPUS_MCP_COMMAND 不能为空")
        return make_mcp_settings()

    monkeypatch.setattr(client_module, "get_mcp_settings", _settings)
    reset_mcp_client()

    try:
        with pytest.raises(ConfigurationError):
            get_mcp_client()

        # 配置修复后：同一个入口直接成功，拿到可用客户端
        recovered = get_mcp_client()
        assert isinstance(recovered, StdioMCPClient)
    finally:
        reset_mcp_client()


def test_singleton_cache_is_shared_across_module_access(isolated_singleton) -> None:
    """经 ``app.mcp`` 包导出的符号与模块直连是同一个函数（缓存语义一致）。"""

    from app.mcp import get_mcp_client as exported_get

    assert exported_get is client_module.get_mcp_client
    assert exported_get() is get_mcp_client()
