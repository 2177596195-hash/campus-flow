"""配置层测试：RAG 相关性阈值与校园业务服务地址的读取与校验。

与 ``tests/rag/test_score_threshold.py`` 分工明确：本文件回答
「配置怎么读、非法值会不会被拦住」，那份文件回答「阈值怎么过滤」。

配置读取依赖进程环境，因此每条用例都通过 ``monkeypatch`` 显式给出
自足的环境（含 Embedding 密钥），并清空对应 ``lru_cache``，
保证用例之间互不影响。

另外这里**显式屏蔽仓库根 ``.env`` 的读取**（把 ``load_environment`` 换成空操作）：
否则「默认值」类断言会被开发者本机的 ``.env`` 内容左右——那属于环境耦合，
不是配置逻辑。屏蔽之后，本文件每个用例的输入只有 ``monkeypatch`` 设定的变量。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import settings as settings_module
from app.config.settings import (
    CampusApiSettings,
    ConfigurationError,
    RAGSettings,
    app_root,
    get_campus_api_settings,
    get_rag_settings,
)

THRESHOLD_ENV = "RAG_SCORE_THRESHOLD"
BASE_URL_ENV = "CAMPUS_SERVICE_BASE_URL"
TIMEOUT_ENV = "CAMPUS_SERVICE_TIMEOUT"
STUDENT_ID_ENV = "CAMPUS_STUDENT_ID"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离环境：补齐必填项、屏蔽 ``.env``、并把可选键置为「未配置」。"""

    # 配置逻辑不读开发者本机的 .env，输入只来自本 fixture 与各用例的 setenv
    monkeypatch.setattr(settings_module, "load_environment", lambda: None)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-for-config")
    monkeypatch.delenv(THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(STUDENT_ID_ENV, raising=False)
    get_rag_settings.cache_clear()
    get_campus_api_settings.cache_clear()
    yield
    get_rag_settings.cache_clear()
    get_campus_api_settings.cache_clear()


def _reload(monkeypatch: pytest.MonkeyPatch) -> RAGSettings:
    """清缓存后重新读取 RAG 配置（模拟新进程冷启动）。

    ``get_rag_settings`` 带 ``lru_cache``，不清缓存则环境变量的变更不可见。
    """

    get_rag_settings.cache_clear()
    return get_rag_settings()


# ------------------------------------------------------ 未配置：默认继续为空


def test_threshold_defaults_to_none_when_unset() -> None:
    """未配置 ``RAG_SCORE_THRESHOLD`` 时阈值为 ``None``，即不额外过滤。"""

    assert get_rag_settings().score_threshold is None


def test_blank_threshold_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """空串 / 纯空白等价于未配置（便于在 .env 里保留占位行）。"""

    monkeypatch.setenv(THRESHOLD_ENV, "   ")

    assert _reload(monkeypatch).score_threshold is None


# ------------------------------------------------------------ 合法取值：读入


@pytest.mark.parametrize("raw", ["0", "0.4", "0.75", "1", "1.0"])
def test_threshold_read_from_env(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """合法取值（含 0 与 1 两个端点）被原样读入。"""

    monkeypatch.setenv(THRESHOLD_ENV, raw)

    assert _reload(monkeypatch).score_threshold == float(raw)


# ------------------------------------------------------------ 非法取值：拒绝


@pytest.mark.parametrize("raw", ["1.5", "2", "-0.1", "-1"])
def test_out_of_range_threshold_rejected(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """超出 ``[0, 1]`` 的取值被配置校验拦截。

    阈值比较的是「越大越相关」的相关性分数，落在 ``[0, 1]`` 之外没有意义，
    拦住它可以避免把「距离」误配成阈值后静默失效。
    """

    monkeypatch.setenv(THRESHOLD_ENV, raw)

    with pytest.raises(ConfigurationError):
        _reload(monkeypatch)


@pytest.mark.parametrize("raw", ["abc", "0.4x", "high", "0,4"])
def test_non_numeric_threshold_rejected(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """非数字取值被拒绝，并给出可定位的错误信息。"""

    monkeypatch.setenv(THRESHOLD_ENV, raw)

    with pytest.raises(ConfigurationError) as excinfo:
        _reload(monkeypatch)

    assert THRESHOLD_ENV in str(excinfo.value)


# -------------------------------- 项目当前正式配置：阈值 0.4（回归保护）

#: 项目当前**正式启用**的相关性阈值（与仓库根 ``.env`` 保持一致）。
#: 取值来自真实数据标定：正样本相似度 0.584–0.768、负样本 0.176–0.35，
#: ``0.4`` 是安全分割点（详见 ``.env.example`` 的阈值说明）。
CURRENT_THRESHOLD = 0.4

#: 仓库根的**非敏感**配置模板（可入库，不含任何真实密钥）。
ENV_EXAMPLE_PATH = app_root().parent / ".env.example"


def _active_env_value(path: Path, key: str) -> str | None:
    """取配置文件里某个键**生效**的取值（忽略注释行与空行）。"""

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def test_current_project_threshold_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """当前正式配置 ``0.4`` 被原样读入（配置层不做任何二次加工）。"""

    monkeypatch.setenv(THRESHOLD_ENV, "0.4")

    assert _reload(monkeypatch).score_threshold == CURRENT_THRESHOLD


def test_config_template_pins_the_current_threshold() -> None:
    """非敏感模板必须声明当前正式阈值，防止模板与运行配置悄悄漂移。"""

    assert ENV_EXAMPLE_PATH.is_file()
    assert _active_env_value(ENV_EXAMPLE_PATH, THRESHOLD_ENV) == "0.4"


def test_config_template_threshold_passes_config_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模板里写的取值必须真的能通过配置校验（模板不允许出现非法示例）。"""

    raw = _active_env_value(ENV_EXAMPLE_PATH, THRESHOLD_ENV)
    assert raw is not None

    monkeypatch.setenv(THRESHOLD_ENV, raw)

    assert _reload(monkeypatch).score_threshold == CURRENT_THRESHOLD


def test_config_template_holds_no_real_secrets() -> None:
    """模板只放占位符：任何带 ``KEY`` 的键取值必须是 ``<...>`` 形式或留空。"""

    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = (part.strip() for part in line.partition("="))
        if "KEY" not in name:
            continue
        assert value == "" or (value.startswith("<") and value.endswith(">")), (
            f"模板里的 {name} 必须留空或写成 <占位符>，不得存放真实密钥"
        )


# ============================================================================
# 校园业务服务配置（CAMPUS_SERVICE_BASE_URL / CAMPUS_SERVICE_TIMEOUT /
# CAMPUS_STUDENT_ID）
# ============================================================================


def _reload_campus(monkeypatch: pytest.MonkeyPatch) -> CampusApiSettings:
    """清缓存后重新读取校园业务服务配置（模拟新进程冷启动）。"""

    get_campus_api_settings.cache_clear()
    return get_campus_api_settings()


def test_campus_defaults_are_usable_without_any_env() -> None:
    """不配置任何环境变量也能跑：给出可用的默认地址与超时。"""

    settings = get_campus_api_settings()

    assert settings.base_url == "http://localhost:8080"
    assert settings.timeout > 0
    assert settings.default_student_id


def test_campus_env_overrides_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """三个键都能被环境变量覆盖。"""

    monkeypatch.setenv(BASE_URL_ENV, "http://127.0.0.1:8081")
    monkeypatch.setenv(TIMEOUT_ENV, "2.5")
    monkeypatch.setenv(STUDENT_ID_ENV, "student-042")

    settings = _reload_campus(monkeypatch)

    assert settings.base_url == "http://127.0.0.1:8081"
    assert settings.timeout == 2.5
    assert settings.default_student_id == "student-042"


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1:8081/",
        "http://127.0.0.1:8081///",
    ],
)
def test_campus_base_url_trailing_slash_is_stripped(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """尾部斜杠被规整掉，避免拼出 ``//api/exams`` 这类路径。"""

    monkeypatch.setenv(BASE_URL_ENV, raw)

    assert _reload_campus(monkeypatch).base_url == "http://127.0.0.1:8081"


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1:8081",        # 缺协议头
        "localhost",             # 只有主机名
        "ftp://127.0.0.1",       # 不支持的协议
        "   ",                   # 只有空白
    ],
)
def test_campus_invalid_base_url_rejected(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """地址非法时被配置校验拦截，而不是等到发请求才报错。"""

    monkeypatch.setenv(BASE_URL_ENV, raw)

    with pytest.raises(ConfigurationError) as excinfo:
        _reload_campus(monkeypatch)

    assert BASE_URL_ENV in str(excinfo.value)


@pytest.mark.parametrize("raw", ["0", "-1", "-0.5"])
def test_campus_non_positive_timeout_rejected(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """非正数超时没有意义（会立刻超时），配置期即拦截。"""

    monkeypatch.setenv(TIMEOUT_ENV, raw)

    with pytest.raises(ConfigurationError) as excinfo:
        _reload_campus(monkeypatch)

    assert TIMEOUT_ENV in str(excinfo.value)


@pytest.mark.parametrize("raw", ["abc", "5s", "1,5"])
def test_campus_non_numeric_timeout_rejected(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时必须是数字。"""

    monkeypatch.setenv(TIMEOUT_ENV, raw)

    with pytest.raises(ConfigurationError) as excinfo:
        _reload_campus(monkeypatch)

    assert TIMEOUT_ENV in str(excinfo.value)


def test_campus_blank_student_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """学生标识不能是空白：它要作为查询条件发给业务服务。"""

    monkeypatch.setenv(STUDENT_ID_ENV, "   ")

    with pytest.raises(ConfigurationError) as excinfo:
        _reload_campus(monkeypatch)

    assert STUDENT_ID_ENV in str(excinfo.value)
