"""把本地评估数据集同步到 LangSmith Dataset 的测试（完全离线）。

本阶段只做一件事：**本地 retrieval_cases.json -> LangSmith Dataset**。
因此这里验证五组事：

==============================  ================================================
要验证的事                       对应用例
==============================  ================================================
映射口径                         ``build_example_spec*``：inputs / outputs /
                                metadata 逐字段，负样本不丢
数据集读取                       内置数据集能读、条数自洽、标识唯一
同步幂等（替身客户端）            首次建、二次什么都不写、已存在则复用、
                                内容变了原地更新、远端多余只报告不删除
配置与 CLI                       未配置时提示明确、Key 不落日志、
                                普通对话绝不触发上传
架构约束                        同步模块不碰评估器 / 检索器 / 向量库，
                                SDK 只在函数内 import
==============================  ================================================

**全程不碰网络**：同步模块把客户端作为参数注入，这里注入的是
``tests/_fakes.py::FakeDatasetClient``（只实现同步用到的六个方法，连 delete 都没有）。
唯一会 import 官方 SDK 的路径是 ``build_dataset_client()``，本文件里也把
``langsmith.Client`` 换成记录参数的构造器——因此没有任何用例会连上 LangSmith。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import main as cli
from app.config import settings as settings_module
from app.config.settings import ConfigurationError, get_langsmith_settings
from app.rag.evaluation import load_dataset
from app.rag.evaluation import langsmith_dataset as dataset_sync_module
from app.rag.evaluation.langsmith_dataset import (
    DATASET_DESCRIPTION,
    DEFAULT_DATASET_NAME,
    METADATA_CASE_ID,
    METADATA_IS_NEGATIVE,
    METADATA_KEYWORDS,
    METADATA_MIN_SCORE,
    DatasetSyncError,
    build_dataset_client,
    build_example_spec,
    build_example_specs,
    case_identifier,
    sync_rag_dataset,
)
from app.rag.evaluation.schema import EvaluationCase
from tests._fakes import FakeDatasetClient

#: 同步模块源码路径。与两条架构守卫里的豁免路径必须是同一处，
#: 因此这里按「相对 app 根目录」的写法断言，路径变了就会当场失败。
_EXPECTED_RELATIVE_PATH: tuple[str, ...] = ("app", "rag", "evaluation", "langsmith_dataset.py")

#: 与 LangSmith 相关的全部环境变量：用例里统一清空，避免读到本机 .env 的真实配置
_LANGSMITH_KEYS: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_PROJECT",
    "LANGCHAIN_ENDPOINT",
    "LANGCHAIN_SESSION",
)

#: 测试用的假密钥。**不是**真实凭证，只用于断言「不外泄」。
FAKE_API_KEY = "lsv2-test-key-not-a-real-credential"


# ------------------------------------------------------------------ 夹具与工具


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离环境：不读本机 ``.env``、清空 LangSmith 变量、清掉配置缓存。

    本机 ``.env`` 里配着真实的 LangSmith Key；若不清掉 ``load_environment``，
    「未配置时的行为」这条用例会静默地按真实凭证走——那正是离线测试最怕的事。
    """

    monkeypatch.setattr(settings_module, "load_environment", lambda: None)
    for key in _LANGSMITH_KEYS:
        monkeypatch.delenv(key, raising=False)
    _reset_caches()


def _reset_caches() -> None:
    """清掉「环境变量 -> 配置」的缓存（配置入口是 ``lru_cache``）。"""

    get_langsmith_settings.cache_clear()


def make_case(question: str, *sources: str, **kwargs: Any) -> EvaluationCase:
    """构造一条用例。"""

    return EvaluationCase(question=question, expected_sources=tuple(sources), **kwargs)


def _first_positive(cases: list[EvaluationCase]) -> EvaluationCase:
    """取第一条正样本（负样本没有期望来源，改不出「内容变化」）。"""

    return next(case for case in cases if not case.is_negative)


# ======================================================= 1. 映射口径（纯函数）


def test_question_goes_to_inputs() -> None:
    """input 用的是用例的问题原文，键名沿用本地字段名 ``question``。"""

    spec = build_example_spec(make_case("图书馆开放时间？", "campus_rules.md"))

    assert spec.inputs == {"question": "图书馆开放时间？"}


def test_expected_sources_go_to_outputs() -> None:
    """output 用的是期望来源，顺序与数据集一致（顺序是人工维护的强弱信号）。"""

    spec = build_example_spec(
        make_case("图书馆和体育馆？", "campus_rules.md", "campus_sports.pdf")
    )

    assert spec.outputs == {"expected_sources": ["campus_rules.md", "campus_sports.pdf"]}


def test_metadata_carries_the_rest_of_the_local_fields() -> None:
    """其余字段进 metadata，且不夹带任何新造的业务字段。"""

    spec = build_example_spec(
        make_case(
            "图书馆开放时间？",
            "campus_rules.md",
            case_id="library-hours",
            expected_keywords=("开放时间", "22:00"),
            expected_min_score=0.5,
        )
    )

    assert spec.metadata == {
        METADATA_CASE_ID: "library-hours",
        METADATA_KEYWORDS: ["开放时间", "22:00"],
        METADATA_MIN_SCORE: 0.5,
        METADATA_IS_NEGATIVE: False,
    }


def test_negative_sample_keeps_empty_sources_and_is_flagged() -> None:
    """负样本不丢失：期望来源仍是空数组，并且有一个能直接筛选的显式标记。"""

    spec = build_example_spec(make_case("今天北京的天气怎么样？", case_id="off-topic"))

    assert spec.outputs == {"expected_sources": []}
    assert spec.metadata[METADATA_IS_NEGATIVE] is True
    assert spec.metadata[METADATA_MIN_SCORE] is None
    assert spec.metadata[METADATA_KEYWORDS] == []


def test_case_identifier_falls_back_to_question() -> None:
    """没有 ``case_id`` 时用问题原文作标识（与报告里的用例名同一规则）。"""

    assert case_identifier(make_case("没有标识的问题", "a.md")) == "没有标识的问题"
    assert case_identifier(make_case("有标识", "a.md", case_id=" c-1 ")) == "c-1"


def test_duplicate_identifiers_are_rejected() -> None:
    """标识重复会让「远端该更新哪一条」失去唯一答案，必须当场拒绝。"""

    with pytest.raises(DatasetSyncError) as excinfo:
        build_example_specs(
            [
                make_case("问题一", "a.md", case_id="same"),
                make_case("问题二", "b.md", case_id="same"),
            ]
        )

    assert "same" in str(excinfo.value)


def test_bundled_dataset_reads_and_maps_one_to_one() -> None:
    """内置数据集能读出来，且每条用例都能映射成一条 Example。"""

    cases = load_dataset()

    assert len(cases) >= 10, "内置数据集应至少覆盖 10 条用例"
    assert sum(1 for case in cases if case.is_negative) >= 1, "数据集必须保留负样本"

    specs = build_example_specs(cases)

    assert len(specs) == len(cases)
    assert [spec.inputs["question"] for spec in specs] == [case.question for case in cases]
    assert len({spec.identifier for spec in specs}) == len(specs)


# ======================================================= 2. 同步（替身客户端）


def test_first_sync_creates_dataset_and_all_examples() -> None:
    """首次同步：建 Dataset + 写入全部用例，负样本原样落地。"""

    client = FakeDatasetClient()

    result = sync_rag_dataset(client=client)

    assert result.dataset_name == DEFAULT_DATASET_NAME
    assert result.dataset_created is True
    assert client.create_dataset_calls == [DEFAULT_DATASET_NAME]
    assert client.descriptions[DEFAULT_DATASET_NAME] == DATASET_DESCRIPTION

    stored = client.examples_of(DEFAULT_DATASET_NAME)
    assert len(stored) == result.cases == result.created
    assert result.updated == 0 and result.unchanged == 0
    assert result.total_examples == len(stored)

    negatives = [item for item in stored if item.metadata[METADATA_IS_NEGATIVE] is True]
    assert len(negatives) == result.negatives >= 1
    for item in negatives:
        assert item.outputs == {"expected_sources": []}

    # 逐条比对「远端存下来的」与「本地数据集里的」，而不是只看条数
    by_identifier = {case_identifier(case): case for case in load_dataset()}
    for item in stored:
        local = by_identifier[item.metadata[METADATA_CASE_ID]]
        assert item.inputs == {"question": local.question}
        assert item.outputs == {"expected_sources": list(local.expected_sources)}
        assert item.metadata[METADATA_KEYWORDS] == list(local.expected_keywords)
        assert item.metadata[METADATA_MIN_SCORE] == local.expected_min_score


def test_second_sync_writes_nothing_at_all() -> None:
    """重复同步不会无限增加 Example：第二次既不建也不写，条数恒定。"""

    client = FakeDatasetClient()
    first = sync_rag_dataset(client=client)

    after_first = len(client.examples)
    writes_after_first = (list(client.create_dataset_calls), list(client.create_examples_calls))

    second = sync_rag_dataset(client=client)

    assert second.dataset_created is False
    assert second.created == 0 and second.updated == 0
    assert second.unchanged == first.cases
    assert len(client.examples) == after_first
    assert (client.create_dataset_calls, client.create_examples_calls) == writes_after_first
    assert client.update_example_calls == []
    assert second.dataset_id == first.dataset_id


def test_existing_dataset_is_reused_instead_of_recreated() -> None:
    """已有同名 Dataset 时复用（沿用其 id），只补写缺失的 Example。"""

    client = FakeDatasetClient()
    existing = client.create_dataset(DEFAULT_DATASET_NAME, description="人工建的同名数据集")
    client.create_dataset_calls.clear()

    cases = load_dataset()
    first_case = cases[0]
    client.seed_example(
        DEFAULT_DATASET_NAME,
        inputs={"question": first_case.question},
        outputs={"expected_sources": list(first_case.expected_sources)},
        metadata={
            METADATA_CASE_ID: case_identifier(first_case),
            METADATA_KEYWORDS: list(first_case.expected_keywords),
            METADATA_MIN_SCORE: first_case.expected_min_score,
            METADATA_IS_NEGATIVE: first_case.is_negative,
        },
    )
    client.create_examples_calls.clear()

    result = sync_rag_dataset(client=client)

    assert client.create_dataset_calls == [], "不该重复创建同名 Dataset"
    assert result.dataset_created is False
    assert result.dataset_id == existing.id
    assert result.created == len(cases) - 1
    assert result.unchanged == 1
    assert client.descriptions[DEFAULT_DATASET_NAME] == "人工建的同名数据集", "不应改动既有描述"


def test_case_with_stable_id_is_updated_not_duplicated() -> None:
    """标识稳定的用例：内容变化走 update，条数不变、id 不变。"""

    client = FakeDatasetClient()
    original = make_case("图书馆开放时间？", "campus_rules.md", case_id="library-hours")
    first = sync_rag_dataset([original], client=client)
    example_id = client.examples_of(DEFAULT_DATASET_NAME)[0].id

    edited = make_case("图书馆开放时间？", "campus_faq.txt", case_id="library-hours")
    second = sync_rag_dataset([edited], client=client)

    assert first.created == 1
    assert second.created == 0 and second.updated == 1 and second.unchanged == 0
    assert client.update_example_calls == [example_id]
    stored = client.examples_of(DEFAULT_DATASET_NAME)
    assert len(stored) == 1
    assert stored[0].id == example_id
    assert stored[0].outputs == {"expected_sources": ["campus_faq.txt"]}


def test_edited_bundled_case_is_updated_in_place() -> None:
    """真实数据集上改一条期望来源：只更新那一条，其余全部跳过。"""

    cases = load_dataset()
    target = _first_positive(cases)
    client = FakeDatasetClient()
    sync_rag_dataset(cases, client=client)
    before = len(client.examples)

    edited = [
        replace(case, expected_sources=("campus_faq.txt",)) if case is target else case
        for case in cases
    ]
    result = sync_rag_dataset(edited, client=client)

    assert result.created == 0
    assert result.updated == 1
    assert result.unchanged == len(cases) - 1
    assert len(client.examples) == before
    updated = next(
        item
        for item in client.examples_of(DEFAULT_DATASET_NAME)
        if item.metadata[METADATA_CASE_ID] == case_identifier(target)
    )
    assert updated.outputs == {"expected_sources": ["campus_faq.txt"]}


def test_remote_only_examples_are_reported_and_kept() -> None:
    """远端多出来的条目只报告、不删除：删除不可逆，本阶段不碰。"""

    client = FakeDatasetClient()
    sync_rag_dataset(client=client)
    baseline = len(client.examples)

    # 一条「不是本工具写的」（读不到 case_id）——人工在平台上加的
    client.seed_example(DEFAULT_DATASET_NAME, inputs={"question": "手工加的用例"})
    # 一条「本工具写的但本地已删掉」——标识读得到，但本地数据集里没有
    client.seed_example(
        DEFAULT_DATASET_NAME,
        inputs={"question": "历史用例"},
        outputs={"expected_sources": ["old.md"]},
        metadata={METADATA_CASE_ID: "legacy-case"},
    )

    result = sync_rag_dataset(client=client)

    assert result.remote_only == 2
    assert result.created == 0 and result.updated == 0
    assert len(client.examples) == baseline + 2
    assert result.total_examples == result.cases + 2


def test_foreign_example_is_never_rewritten() -> None:
    """读不到标识的远端条目不会被当成「本地用例」改写。"""

    client = FakeDatasetClient()
    sync_rag_dataset(client=client)
    foreign = client.seed_example(
        DEFAULT_DATASET_NAME, inputs={"question": "手工加的用例"}, metadata={"owner": "human"}
    )

    sync_rag_dataset(client=client)

    assert foreign.id not in client.update_example_calls
    assert client.examples[foreign.id].metadata == {"owner": "human"}


def test_listing_happens_before_any_write() -> None:
    """同步是「先读后写」：先拉全量远端状态，再写，避免边写边读。"""

    order: list[str] = []

    class OrderRecordingClient(FakeDatasetClient):
        def list_examples(self, dataset_id: Any) -> Any:
            order.append("list")
            return super().list_examples(dataset_id)

        def create_examples(self, *, dataset_id: Any, examples: Any) -> Any:
            order.append("create")
            return super().create_examples(dataset_id=dataset_id, examples=examples)

    sync_rag_dataset(client=OrderRecordingClient())

    assert order == ["list", "create"]


# ======================================================= 3. 配置与密钥


def test_missing_api_key_gives_a_clear_error() -> None:
    """未配置 LangSmith：报错说清缺什么、去哪配，而不是抛底层异常。"""

    with pytest.raises(ConfigurationError) as excinfo:
        build_dataset_client()

    message = str(excinfo.value)
    assert "LANGSMITH_API_KEY" in message
    assert "未配置" in message


def test_client_uses_configured_key_and_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端用第一阶段同一份配置装配；显式传入，不看追踪开关的脸色。"""

    import langsmith

    monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://example.invalid/api/")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    _reset_caches()

    recorded: dict[str, Any] = {}

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            recorded.update(kwargs)

    monkeypatch.setattr(langsmith, "Client", RecordingClient)

    build_dataset_client()

    assert recorded["api_key"] == FAKE_API_KEY
    # endpoint 的尾部斜杠在配置入口已归一化
    assert recorded["api_url"] == "https://example.invalid/api"


def test_invalid_endpoint_error_never_leaks_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置非法时同样只报变量名，不回显任何密钥片段。"""

    monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "ftp://example.invalid")
    _reset_caches()

    with pytest.raises(ConfigurationError) as excinfo:
        build_dataset_client()

    message = str(excinfo.value)
    assert "LANGSMITH_ENDPOINT" in message
    assert FAKE_API_KEY not in message


def test_sync_output_and_logs_never_contain_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """同步成功时的输出与日志里都不得出现密钥。"""

    import langsmith

    monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
    _reset_caches()
    monkeypatch.setattr(langsmith, "Client", lambda **kwargs: FakeDatasetClient())
    caplog.set_level(logging.DEBUG)

    assert cli._run_sync_rag_dataset() == 0

    texts = [record.getMessage() for record in caplog.records] + [capsys.readouterr().out]
    assert all(FAKE_API_KEY not in text for text in texts)


# ======================================================= 4. CLI 接线


def test_flag_defaults_to_off() -> None:
    """默认不上传：只有显式参数才会同步数据集。"""

    assert cli._parse_args([]).sync_rag_dataset is False
    assert cli._parse_args(["你好"]).sync_rag_dataset is False
    assert cli._parse_args(["--sync-rag-dataset"]).sync_rag_dataset is True


def test_normal_run_never_touches_the_sync(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """普通一轮对话照旧：不建客户端、不读 LangSmith 配置、不上传。"""

    monkeypatch.setattr(
        cli, "build_dataset_client", lambda: pytest.fail("普通对话不该碰数据集同步")
    )

    class FakeRuntime:
        def invoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
            return {
                "messages": [AIMessage(content="你好，我是 CampusFlow。")],
                "intent": "chat",
            }

    assert cli._run_once(FakeRuntime(), "你好", "session-1") == 0
    assert "你好，我是 CampusFlow。" in capsys.readouterr().out


def test_main_does_not_sync_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python main.py "xxx"`` 这条路径上不存在同步动作。"""

    observed = _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["你好"]) == 0
    assert observed["sync"] == []
    assert observed["runtime"] == [True], "普通提问仍要装配运行时"


def test_main_dispatches_sync_when_flag_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式传参时才走到同步分支，且**不**装配 Agent 运行时。"""

    observed = _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["--sync-rag-dataset"]) == 0
    assert observed["sync"] == [True]
    assert observed["runtime"] == []


def _patch_main_for_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[bool]]:
    """把 ``main()`` 的周围依赖换成轻薄替身，只观察「派发去了哪里」。"""

    observed: dict[str, list[bool]] = {"sync": [], "runtime": []}
    settings = SimpleNamespace(
        app_name="campus-flow",
        environment="test",
        log_level="INFO",
        llm=SimpleNamespace(provider="fake", model="fake"),
        checkpointer_backend="memory",
    )

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_log_startup", lambda settings: None)
    monkeypatch.setattr(cli, "apply_langsmith_tracing", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "_build_runtime",
        lambda settings: (observed["runtime"].append(True), object())[1],
    )
    monkeypatch.setattr(
        cli, "_run_sync_rag_dataset", lambda: (observed["sync"].append(True), 0)[1]
    )
    monkeypatch.setattr(cli, "_run_once", lambda *args: 0)
    return observed


def test_sync_entry_point_reports_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未配置 LangSmith 时 CLI 给清晰提示并以配置错误码退出（不是崩溃）。"""

    exit_code = cli._run_sync_rag_dataset()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "配置错误" in captured.err
    assert "LANGSMITH_API_KEY" in captured.err
    assert FAKE_API_KEY not in captured.err


def test_sync_entry_point_prints_dataset_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 报告必须给出 Dataset 名称 / id / 条数。"""

    import langsmith

    client = FakeDatasetClient()
    monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
    _reset_caches()
    monkeypatch.setattr(langsmith, "Client", lambda **kwargs: client)

    assert cli._run_sync_rag_dataset() == 0

    report = capsys.readouterr().out
    assert DEFAULT_DATASET_NAME in report
    assert client.dataset_id(DEFAULT_DATASET_NAME) in report
    assert f"远端总条数: {len(client.examples)}" in report
    assert "状态: 新建" in report


# ======================================================= 5. 架构约束（守卫）


def test_module_path_matches_the_guard_exemption() -> None:
    """两条架构守卫按路径豁免本模块，路径写错等于豁免失效。"""

    expected = Path(settings_module.app_root()).joinpath(*_EXPECTED_RELATIVE_PATH)

    assert Path(dataset_sync_module.__file__) == expected
    assert dataset_sync_module.__file__.endswith("langsmith_dataset.py")


def test_sync_module_keeps_the_evaluation_layer_clean() -> None:
    """同步模块只搬数据：不 import 评估器 / 检索器 / 向量库 / Agent。"""

    source = Path(dataset_sync_module.__file__).read_text(encoding="utf-8")

    for token in (
        "app.rag.evaluation.evaluator",
        "KnowledgeRetriever",
        "vectorstore",
        "embeddings",
        "app.agent",
        "ChatOpenAI",
        "_collection",
    ):
        assert token not in source, f"同步模块越界依赖了 {token}"


def test_sdk_is_imported_lazily() -> None:
    """SDK 与配置入口都在函数内 import：模块级零副作用，普通运行不受影响。"""

    source = Path(dataset_sync_module.__file__).read_text(encoding="utf-8")
    top_level_imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]

    assert not any("langsmith" in line for line in top_level_imports), top_level_imports
    assert "from app.config.settings import" not in top_level_imports, "配置入口也应是惰性 import"
