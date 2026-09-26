"""pytest 全局夹具。

这里只做两件事：**保证测试永远不会真的去连校园业务服务**，
以及**保证测试永远不会真的把 trace 上报到 LangSmith 在线服务**。

为什么放在全局 conftest 而不是某个测试模块：任何测试（包括以后新增的）
都不应该因为「本机恰好开着 Spring Boot」而出现不同结果，也不应该在 CI
上因为缺少外部服务而失败。默认把校园业务服务客户端替换成挂在
``httpx.MockTransport`` 上的替身；需要非默认响应（5xx、超时、畸形 JSON 等）
的用例，在自己的夹具里再次替换对应工具模块的 ``get_campus_api_client``
即可（后设置者生效）。

替身按**请求路径**分派，三条接口各自的响应都与 MySQL 种子数据一致：

- ``/api/exams`` → 考试安排；
- ``/api/courses`` → 课表，并**按 ``week`` 查询参数过滤**，与 Spring Boot
  的 ``CourseService`` 行为一致；
- ``/api/scores`` → 成绩，并**按 ``semester`` 查询参数过滤**，与 Spring Boot
  的 ``ScoreService`` 行为一致。

之所以要模拟服务端过滤：过滤现在是服务端的职责，若替身对任何条件都返回同一份
数据，「参数是否真的传下去了」就无法被察觉。

替身本身不构成对服务端行为的证明：周次与学期的过滤语义由
``CourseScheduleRepositoryTest`` / ``CourseServiceTest`` /
``CourseControllerTest`` / ``ScoreRepositoryTest`` / ``ScoreServiceTest`` /
``ScoreControllerTest``（真实 MySQL）以及各阶段的真实联调覆盖。

LangSmith 的守卫同理（``offline_langsmith``）：追踪只要开着，每一次模型与工具
调用都会被上报，仓库根 ``.env`` 里又确实配着真实的密钥。因此会话级把开关
强制关掉——需要验证「开启追踪」的用例（``tests/test_langsmith_tracing.py``）
在自己的夹具里显式覆盖，并且把上报地址指向本机回环 stub。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests._fakes import (
    DEFAULT_CAMPUS_PAYLOADS,
    make_campus_client,
    patch_campus_api_for_tools,
)

#: 与「是否上报」有关的环境变量。SDK 判断开关时**优先**读 ``*_TRACING_V2``，
#: 所以只关 ``LANGSMITH_TRACING`` 不够，必须把旧名一并清掉。
_TRACING_SWITCH_KEYS: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
)


@pytest.fixture(autouse=True)
def offline_campus_api(monkeypatch: pytest.MonkeyPatch):
    """离线校园业务服务替身（默认响应与 MySQL 种子数据一致）。"""

    return patch_campus_api_for_tools(
        monkeypatch, make_campus_client(payloads=DEFAULT_CAMPUS_PAYLOADS)
    )


@pytest.fixture(autouse=True, scope="session")
def offline_langsmith() -> Iterator[None]:
    """整套测试默认关闭 LangSmith 追踪（会话级，先于所有用例生效）。

    不这样做的话存在一条隐蔽路径：某个用例若真的加载了 ``.env``
    （忘了屏蔽 ``load_environment``），``LANGSMITH_TRACING`` 就会进入进程环境，
    之后**所有**跑图与调模型的用例都会把 trace 发到线上——又慢又不该发生。

    这里直接改环境（会话级夹具不能用函数级的 ``monkeypatch``），并在会话结束时
    还原成进入时的取值；同时清掉官方 SDK 的环境读取缓存，避免先前的读取把
    「关掉」这个事实挡住。
    """

    saved = {key: os.environ.get(key) for key in _TRACING_SWITCH_KEYS}
    for key in _TRACING_SWITCH_KEYS:
        os.environ.pop(key, None)
    os.environ["LANGSMITH_TRACING"] = "false"
    _clear_sdk_env_cache()
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_sdk_env_cache()


def _clear_sdk_env_cache() -> None:
    """清掉官方 SDK 对「读环境变量」结果的缓存。

    ``langsmith.utils.get_env_var`` / ``get_tracer_project`` 是 ``lru_cache``，
    环境变量改了但缓存没清，SDK 仍会按旧值决定要不要上报。
    """

    try:
        from langsmith import utils as langsmith_utils
    except ImportError:  # pragma: no cover - 依赖链缺失时无缓存可清
        return
    langsmith_utils.get_env_var.cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()
