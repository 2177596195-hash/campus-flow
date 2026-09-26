"""RAG 离线评估测试（完全离线）。

覆盖三类关注点：

1. **指标口径**：hit@1 / hit@k / precision / recall 的定义与边界
   （负样本、零召回、多期望来源、k 小于期望来源数）；这部分用 ``score_case``
   直接喂片段，不经过任何向量库，确保口径本身可被独立验证。
2. **端到端**：``question -> Retriever -> 指标``，用按查询分派的替身向量库驱动，
   验证数据集评估、汇总、序列化与报告渲染。
3. **架构守卫**：评估层不得越过 Retriever 去碰向量库 / Embedding / Agent / LLM，
   也不得触碰后端私有属性。

另有一条真实 Chroma（哈希向量，仍离线）的集成用例，验证整条评估链路在真实后端上可用。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_chroma import Chroma

from app.config.settings import app_root
from app.rag.evaluation import (
    DEFAULT_HIT_K,
    DatasetError,
    EvaluationCase,
    default_dataset_path,
    evaluate_dataset,
    evaluate_retrieval,
    load_dataset,
    parse_cases,
    score_case,
    summarize_dataset,
)
from app.rag.evaluation import evaluator as evaluator_module
from app.rag.retriever import KnowledgeRetriever
from app.rag.schema import RetrievedChunk
from tests._fakes import HashingEmbeddings, ScriptedVectorStore, make_document

#: 唯一被允许 import LangSmith 的评估层文件：它做的是「把数据集同步到远端 Dataset」
#: 这件运维动作，不参与任何指标计算（详见 tests/test_langsmith_tracing.py 的同类豁免说明）。
_DATASET_SYNC_MODULE: str = "langsmith_dataset.py"

# --------------------------------------------------------------- 测试用构造


def chunk(source: str, score: float | None, *, content: str = "") -> RetrievedChunk:
    """构造一条召回片段。"""

    return RetrievedChunk(
        content=content or f"{source} 的正文内容",
        source=source,
        title=source,
        score=score,
    )


def case(question: str, *sources: str, **kwargs: object) -> EvaluationCase:
    """构造一条评估用例。"""

    return EvaluationCase(question=question, expected_sources=tuple(sources), **kwargs)  # type: ignore[arg-type]


def make_retriever(store: ScriptedVectorStore, *, top_k: int = DEFAULT_HIT_K) -> KnowledgeRetriever:
    """把替身向量库包装成评估实际使用的检索器。"""

    return KnowledgeRetriever(store, top_k=top_k)


# --------------------------------------------------------------- 1. 指标口径


def test_hit_at_1_true_when_top1_source_is_expected() -> None:
    """Top 1 命中期望来源时 hit@1 为真。"""

    result = score_case(
        case("图书馆开放时间？", "campus_rules.md"),
        [chunk("campus_rules.md", 0.9), chunk("campus_faq.txt", 0.5)],
        k=3,
    )

    assert result.hit_at_1 is True
    assert result.hit_at_k is True
    assert result.first_relevant_rank == 1


def test_hit_at_1_false_when_expected_source_is_not_top1() -> None:
    """期望来源排在第二位时 hit@1 为假，但 hit@k 仍为真。"""

    result = score_case(
        case("图书馆开放时间？", "campus_rules.md"),
        [chunk("campus_faq.txt", 0.9), chunk("campus_rules.md", 0.8)],
        k=3,
    )

    assert result.hit_at_1 is False
    assert result.hit_at_k is True
    assert result.first_relevant_rank == 2


def test_hit_at_k_respects_window() -> None:
    """k 之外的命中不计入 hit@k。"""

    chunks = [
        chunk("campus_faq.txt", 0.9),
        chunk("campus_sports.pdf", 0.8),
        chunk("campus_rules.md", 0.7),
    ]
    expected = "campus_rules.md"

    assert score_case(case("问题", expected), chunks, k=3).hit_at_k is True
    assert score_case(case("问题", expected), chunks, k=2).hit_at_k is False


def test_precision_is_share_of_expected_sources_among_returned() -> None:
    """precision = 返回来源中命中期望的比例（此处 2/3）。"""

    result = score_case(
        case("图书馆和奖学金？", "campus_rules.md", "scholarship.md"),
        [
            chunk("campus_rules.md", 0.9),
            chunk("scholarship.md", 0.8),
            chunk("campus_faq.txt", 0.7),
        ],
        k=3,
    )

    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(1.0)


def test_precision_deduplicates_repeated_sources() -> None:
    """同一文档的多个片段只算一个来源，不因片段多而抬高 precision。"""

    result = score_case(
        case("图书馆开放时间？", "campus_rules.md"),
        [
            chunk("campus_rules.md", 0.9),
            chunk("campus_rules.md", 0.8),
            chunk("campus_rules.md", 0.7),
        ],
        k=3,
    )

    assert result.actual_sources == ("campus_rules.md",)
    assert result.precision == pytest.approx(1.0)
    assert result.retrieved == 3


def test_recall_is_partial_for_multiple_expected_sources() -> None:
    """expected_sources 有多个时只召回其一，recall 为部分命中（1/2）。"""

    result = score_case(
        case("图书馆和体育馆的开放时间？", "campus_rules.md", "campus_sports.pdf"),
        [chunk("campus_rules.md", 0.9), chunk("campus_faq.txt", 0.4)],
        k=3,
    )

    assert result.recall == pytest.approx(0.5)
    assert result.matched_sources == ("campus_rules.md",)
    assert result.missed_sources == ("campus_sports.pdf",)


def test_top_k_smaller_than_expected_sources_caps_recall() -> None:
    """k 小于期望来源数量时，recall 天然受限但仍可计算，不抛异常。"""

    result = score_case(
        case("跨文档问题", "a.md", "b.md", "c.md"),
        [chunk("a.md", 0.9)],
        k=1,
    )

    assert result.recall == pytest.approx(1 / 3)
    assert result.hit_at_k is True
    assert result.precision == pytest.approx(1.0)


def test_negative_case_has_undefined_precision_and_recall() -> None:
    """expected_sources 为空：hit 为假，precision / recall 未定义（None）。"""

    result = score_case(
        case("今天天气怎么样？"),
        [chunk("campus_faq.txt", 0.3), chunk("campus_rules.md", 0.2)],
        k=3,
    )

    assert result.is_negative is True
    assert result.hit_at_1 is False
    assert result.hit_at_k is False
    assert result.precision is None
    assert result.recall is None
    # 仍记录实际召回了什么，便于判断「本不该命中」的问题是否被误召回
    assert result.actual_sources == ("campus_faq.txt", "campus_rules.md")
    assert result.retrieved == 2


def test_empty_retrieval_scores_zero_not_undefined() -> None:
    """有期望来源但零召回：precision / recall 记为 0.0，而不是未定义。"""

    result = score_case(case("图书馆开放时间？", "campus_rules.md"), [], k=3)

    assert result.retrieved == 0
    assert result.actual_sources == ()
    assert result.precision == 0.0
    assert result.recall == 0.0
    assert result.hit_at_1 is False
    assert result.hit_at_k is False
    assert result.first_relevant_rank is None


def test_scores_align_with_actual_sources() -> None:
    """``scores`` 与 ``actual_sources`` 等长且一一对应，可直接 zip 展示。"""

    result = score_case(
        case("问题", "a.md"),
        [chunk("a.md", 0.9), chunk("b.md", 0.5)],
        k=3,
    )

    assert len(result.scores) == len(result.actual_sources)
    assert dict(zip(result.actual_sources, result.scores)) == {"a.md": 0.9, "b.md": 0.5}


def test_matched_keywords_and_min_score_diagnostics() -> None:
    """关键词与分数下限是辅助诊断：记录但不改变命中判定。"""

    result = score_case(
        EvaluationCase(
            question="图书馆开放时间？",
            expected_sources=("campus_rules.md",),
            expected_keywords=("开放时间", "不存在的词"),
            expected_min_score=0.9,
        ),
        [
            chunk("campus_rules.md", 0.8, content="图书馆开放时间为 8:00 至 22:00。"),
            chunk("campus_faq.txt", 0.4, content="快递服务中心营业时间。"),
        ],
        k=3,
    )

    assert result.matched_keywords == ("开放时间",)
    assert result.min_score_ok is False  # 命中来源的分数 0.8 < 声明的 0.9
    assert result.hit_at_k is True  # 只影响诊断，不改变命中


# ------------------------------------------------- 2. 端到端（替身向量库）


@pytest.fixture
def scripted_store() -> ScriptedVectorStore:
    """三条用例的召回脚本；未登记的查询返回空结果。"""

    return ScriptedVectorStore(
        {
            "图书馆开放时间是怎么规定的？": [
                (make_document("图书馆开放时间 8:00-22:00。", source="campus_rules.md"), 0.10),
                (make_document("快递服务中心营业时间。", source="campus_faq.txt"), 0.60),
            ],
            "图书馆和体育馆的开放时间分别是什么？": [
                (make_document("体育馆每周一至周五开放。", source="campus_sports.pdf"), 0.15),
                (make_document("图书馆开放时间 8:00-22:00。", source="campus_rules.md"), 0.20),
                (make_document("宿舍熄灯时间。", source="campus_rules.md"), 0.70),
            ],
            "今天北京的天气怎么样？": [
                (make_document("校园卡挂失流程。", source="campus_faq.txt"), 0.80),
            ],
        }
    )


def test_evaluate_retrieval_hits_expected_source(scripted_store: ScriptedVectorStore) -> None:
    """``evaluate_retrieval`` 把问题交给检索器并给出正确判定。"""

    result = evaluate_retrieval(
        "图书馆开放时间是怎么规定的？",
        ["campus_rules.md"],
        retriever=make_retriever(scripted_store),
    )

    assert result.hit_at_1 is True
    assert result.actual_sources == ("campus_rules.md", "campus_faq.txt")
    assert result.scores == (0.9, 0.4)  # 距离 0.10 / 0.60 换算后的相关性
    assert scripted_store.queries == ["图书馆开放时间是怎么规定的？"]


def test_evaluate_dataset_summary_metrics(scripted_store: ScriptedVectorStore) -> None:
    """整份数据集的汇总指标与逐条明细一致。"""

    cases = [
        case("图书馆开放时间是怎么规定的？", "campus_rules.md"),
        case("图书馆和体育馆的开放时间分别是什么？", "campus_rules.md", "campus_sports.pdf"),
        case("今天北京的天气怎么样？"),
    ]

    report = evaluate_dataset(cases, retriever=make_retriever(scripted_store))

    assert report.total_cases == 3
    assert report.k == DEFAULT_HIT_K
    assert report.scored_cases == 2
    assert report.no_expected_source_cases == 1
    assert report.hit_at_1 == pytest.approx(1.0)  # 两条正样本 top1 都命中
    assert report.hit_at_k == pytest.approx(1.0)
    # 精度: 第 1 条 1/2（额外召回 faq）；第 2 条 2/2（两个期望来源都召回）
    assert report.precision == pytest.approx((0.5 + 1.0) / 2)
    assert report.recall == pytest.approx(1.0)
    assert report.no_hit_cases == 0
    assert report.failed_cases == ()
    assert report.unexpected_hits == 1  # 负样本仍被召回了片段
    assert len(report.cases) == 3


def test_negative_case_excluded_from_aggregates(scripted_store: ScriptedVectorStore) -> None:
    """负样本不计入 hit / precision / recall 均值，只单独统计。"""

    cases = [
        case("图书馆开放时间是怎么规定的？", "campus_rules.md"),  # 正样本，命中
        case("今天北京的天气怎么样？"),  # 负样本，却被召回
    ]

    report = evaluate_dataset(cases, retriever=make_retriever(scripted_store))

    # 若把负样本按未命中计入均值，hit@1 会变成 50%
    assert report.hit_at_1 == pytest.approx(1.0)
    assert report.hit_at_k == pytest.approx(1.0)
    assert report.scored_cases == 1
    assert report.no_expected_source_cases == 1
    assert report.unexpected_hits == 1


def test_no_hit_case_is_reported_as_failed(scripted_store: ScriptedVectorStore) -> None:
    """完全没命中的用例进入 failed_cases，并计入 no_hit_cases。"""

    cases = [
        case("图书馆开放时间是怎么规定的？", "library.md"),  # 期望来源不存在于召回中
        case("今天北京的天气怎么样？"),
    ]

    report = evaluate_dataset(cases, retriever=make_retriever(scripted_store))

    assert report.no_hit_cases == 1
    assert [failed.label for failed in report.failed_cases] == ["图书馆开放时间是怎么规定的？"]
    assert report.hit_at_1 == pytest.approx(0.0)
    assert report.precision == pytest.approx(0.0)
    assert report.recall == pytest.approx(0.0)
    assert report.passed is False


def test_top_k_controls_hit_window_end_to_end(scripted_store: ScriptedVectorStore) -> None:
    """同一份召回结果，k 变小会让原本命中的用例失败（hit@k 窗口生效）。"""

    cases = [case("图书馆和体育馆的开放时间分别是什么？", "campus_rules.md")]

    assert evaluate_dataset(cases, retriever=make_retriever(scripted_store), top_k=3).hit_at_k == 1.0
    assert evaluate_dataset(cases, retriever=make_retriever(scripted_store), top_k=1).hit_at_k == 0.0


def test_evaluate_dataset_rejects_empty_cases(scripted_store: ScriptedVectorStore) -> None:
    """空数据集直接报错，而不是返回一份全零报告。"""

    with pytest.raises(ValueError, match="数据集为空"):
        evaluate_dataset([], retriever=make_retriever(scripted_store))


def test_zero_retrieval_makes_every_case_miss(scripted_store: ScriptedVectorStore) -> None:
    """未登记查询返回空结果：所有用例零召回，报告如实反映。"""

    cases = [case("知识库之外的问题", "campus_rules.md")]

    report = evaluate_dataset(cases, retriever=make_retriever(scripted_store))

    assert report.retrieved_cases == 0
    assert report.hit_at_k == pytest.approx(0.0)
    assert report.cases[0].retrieved == 0


# --------------------------------------------------------------- 3. 序列化与报告


def test_case_result_serialization_contains_required_fields() -> None:
    """单条结果可序列化，且字段名与需求一致。"""

    result = score_case(
        case("图书馆开放时间？", "campus_rules.md"),
        [chunk("campus_rules.md", 0.9)],
        k=3,
    )
    payload = result.as_dict()

    assert payload["question"] == "图书馆开放时间？"
    assert payload["expected_sources"] == ["campus_rules.md"]
    assert payload["actual_sources"] == ["campus_rules.md"]
    assert payload["scores"] == [0.9]
    assert payload["hit_at_1"] is True
    assert payload["hit_at_k"] is True
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_report_serialization_round_trip(scripted_store: ScriptedVectorStore) -> None:
    """报告可序列化为 JSON 并原样还原关键字段。"""

    report = evaluate_dataset(
        [
            case("图书馆开放时间是怎么规定的？", "campus_rules.md"),
            case("今天北京的天气怎么样？"),
        ],
        retriever=make_retriever(scripted_store),
    )

    restored = json.loads(report.to_json())

    assert restored["total_cases"] == 2
    assert restored["hit_at_1"] == pytest.approx(1.0)
    assert restored["precision"] == pytest.approx(0.5)
    assert restored["no_expected_source_cases"] == 1
    assert len(restored["cases"]) == 2
    assert restored["cases"][0]["question"] == "图书馆开放时间是怎么规定的？"
    assert "".join(report.to_json()) == report.to_json()  # 结果稳定可复现


def test_format_report_contains_headline_metrics(scripted_store: ScriptedVectorStore) -> None:
    """命令行报告包含固定指标行与失败明细。"""

    report = evaluate_dataset(
        [
            case("图书馆开放时间是怎么规定的？", "campus_rules.md"),
            case("图书馆开放时间是怎么规定的？", "library.md"),
            case("今天北京的天气怎么样？"),
        ],
        retriever=make_retriever(scripted_store),
    )
    text = report.format_report()

    assert text.startswith("RAG Evaluation\n")
    assert f"Cases: {report.total_cases}" in text
    assert "Hit@1: 50.0%" in text
    assert f"Hit@{report.k}: 50.0%" in text
    assert "Precision: 25.0%" in text
    assert "Recall: 50.0%" in text
    assert "Failed cases" in text
    assert "No-expected-source cases" in text
    # 未声明分数下限时不应出现分数标定行
    assert "分数标定" not in text


def test_format_report_renders_scores_and_min_score_hint() -> None:
    """失败明细展示期望/实际来源与分数，并提示未达声明下限。"""

    store = ScriptedVectorStore(
        {
            "图书馆开放时间？": [
                (make_document("快递服务中心营业时间。", source="campus_faq.txt"), 0.58),
            ]
        }
    )
    report = evaluate_dataset(
        [
            EvaluationCase(
                question="图书馆开放时间？",
                expected_sources=("library.md",),
                expected_min_score=0.99,
            )
        ],
        retriever=make_retriever(store),
    )
    text = report.format_report()

    assert "expected: library.md" in text
    assert "campus_faq.txt (0.420)" in text  # 距离 0.58 换算为相关性 0.42
    assert "min_score: 未达到用例声明的分数下限" in text
    assert "分数标定: 0/1 达到声明下限" in text
    assert "missed:   library.md" in text


# --------------------------------------------------------------- 4. 数据集加载


def _write_dataset(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_dataset_from_file(tmp_path: Path) -> None:
    """从 JSON 文件加载用例，字段与默认值均按约定解析。"""

    path = _write_dataset(
        tmp_path / "cases.json",
        {
            "version": 1,
            "cases": [
                {"question": "图书馆开放时间？", "expected_sources": ["campus_rules.md"]},
                {
                    "case_id": "neg-1",
                    "question": "今天天气怎么样？",
                    "expected_sources": [],
                    "expected_keywords": ["天气"],
                    "expected_min_score": 0.5,
                },
            ],
        },
    )

    cases = load_dataset(path)

    assert [item.question for item in cases] == ["图书馆开放时间？", "今天天气怎么样？"]
    assert cases[0].expected_sources == ("campus_rules.md",)
    assert cases[0].case_id == ""
    assert cases[0].label == "图书馆开放时间？"
    assert cases[1].is_negative is True
    assert cases[1].expected_keywords == ("天气",)
    assert cases[1].expected_min_score == 0.5
    assert "负样本 1 条" in summarize_dataset(cases)


def test_load_dataset_accepts_bare_list(tmp_path: Path) -> None:
    """数据集根节点为数组时同样可加载。"""

    path = _write_dataset(tmp_path / "list.json", [{"question": "图书馆开放时间？"}])

    cases = load_dataset(path)

    assert len(cases) == 1
    assert cases[0].expected_sources == ()


def test_load_dataset_missing_file_raises(tmp_path: Path) -> None:
    """文件不存在时给出明确错误。"""

    with pytest.raises(DatasetError, match="不存在"):
        load_dataset(tmp_path / "missing.json")


def test_load_dataset_invalid_json_raises(tmp_path: Path) -> None:
    """非法 JSON 时报错并指出文件。"""

    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(DatasetError, match="不是合法 JSON"):
        load_dataset(path)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"cases": []}, "为空"),
        ({"version": 1}, "缺少 cases"),
        ([{"expected_sources": ["a.md"]}], "question"),
        ([{"question": "  ", "expected_sources": ["a.md"]}], "question"),
        ([{"question": "问", "expected_sources": "a.md"}], "必须是数组"),
        ([{"question": "问", "expected_sources": [1]}], "只能包含字符串"),
        ([{"question": "问", "expected_sources": [""]}], "空字符串"),
        ([{"question": "问", "expected_sources": [], "expected_min_score": 2}], "\\[0, 1\\]"),
        ([{"question": "问", "expected_sources": [], "expected_min_score": "高"}], "必须是数字"),
        ({"cases": [{"question": "同问", "expected_sources": []}, {"question": "同问"}]}, "重复问题"),
    ],
)
def test_parse_cases_rejects_invalid_payload(payload: object, match: str) -> None:
    """结构非法时抛出 ``DatasetError``，不静默放过。"""

    with pytest.raises(DatasetError, match=match):
        parse_cases(payload)


def test_bundled_dataset_loads_and_covers_documents() -> None:
    """随仓库提供的数据集可加载，且覆盖多文档、正负样本。"""

    cases = load_dataset()

    assert len(cases) == 15
    assert sum(1 for item in cases if item.is_negative) == 3
    sources = {source for item in cases for source in item.expected_sources}
    assert sources == {"campus_rules.md", "campus_faq.txt", "campus_sports.pdf"}
    # 同一文档存在多个不同问题
    assert sum(1 for item in cases if item.expected_sources == ("campus_rules.md",)) >= 2
    # 至少一条跨文档问题
    assert any(len(item.expected_sources) > 1 for item in cases)
    assert all(item.question.strip() for item in cases)
    assert default_dataset_path().is_file()


def test_bundled_dataset_expected_sources_exist() -> None:
    """数据集声明的来源必须真实存在于知识库目录，避免改名后静默失效。"""

    knowledge_dir = app_root() / "data" / "knowledge"
    missing = [
        source
        for item in load_dataset()
        for source in item.expected_sources
        if not (knowledge_dir / source).is_file()
    ]

    assert missing == [], f"数据集引用了不存在的知识来源: {missing}"


# --------------------------------------------------------------- 5. 架构守卫


def test_evaluation_never_reaches_below_retriever() -> None:
    """评估层不得越过 Retriever 直接接触向量库 / Embedding / Agent / LLM。

    **单文件豁免**（``_DATASET_SYNC_MODULE``）：把数据集同步到 LangSmith Dataset
    是运维动作、不参与评估计算，但必然要 import LangSmith 的 Datasets API。
    豁免只覆盖那一个文件，并且只豁免 ``langsmith`` 一个词——
    向量库 / Embedding / ``app.agent`` / ``ChatOpenAI`` / 后端私有属性对它同样禁用，
    因此「评估层不越过 Retriever」这条约束依旧完整。
    """

    package = Path(evaluator_module.__file__).parent
    forbidden = (
        "chroma",
        "milvus",
        "faiss",
        "qdrant",
        "pinecone",
        "vectorstore",
        "embeddings",
        "app.agent",
        "langsmith",
        "ChatOpenAI",
    )
    #: 文件 -> 该文件被放行的禁用词（其余禁用词照旧生效）
    exempt: dict[str, frozenset[str]] = {_DATASET_SYNC_MODULE: frozenset({"langsmith"})}

    for module in sorted(package.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        allowed = exempt.get(module.name, frozenset())
        import_lines = [
            line for line in source.splitlines() if line.lstrip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            for token in forbidden:
                if token in allowed:
                    continue
                assert token.lower() not in line.lower(), (
                    f"{module.name} 越层依赖了 {token}: {line.strip()}"
                )
        assert "_collection" not in source, f"{module.name} 触碰了后端私有属性"

    # 唯一允许的检索入口就是 Retriever 本身
    guarded = Path(evaluator_module.__file__).read_text(encoding="utf-8")
    assert "KnowledgeRetriever" in guarded
    assert "RAGService" not in guarded


# ------------------------------------------------- 6. 真实向量库（仍离线）


def _build_chroma(persist_dir: Path) -> Chroma:
    """用确定性哈希向量构建真实 Chroma 集合（与生产一致的余弦空间）。"""

    store = Chroma(
        collection_name="evaluation_test",
        embedding_function=HashingEmbeddings(),
        persist_directory=str(persist_dir),
        collection_metadata={"hnsw:space": "cosine"},
    )
    store.add_texts(
        texts=[
            "图书馆的开放时间为周一至周五 8:00 至 22:00，周末 9:00 至 17:00。",
            "学生宿舍禁止使用热得快、电饭锅等大功率电器。",
            "奖学金评定依据综合素质测评成绩排名，按学年组织。",
        ],
        metadatas=[
            {"source": "library.md", "title": "图书馆管理办法"},
            {"source": "campus_rules.md", "title": "宿舍管理规定"},
            {"source": "scholarship.md", "title": "奖学金评定办法"},
        ],
    )
    return store


def test_evaluate_dataset_against_real_chroma(tmp_path: Path) -> None:
    """整条评估链路在真实后端上可用：question -> Retriever -> 指标。"""

    retriever = KnowledgeRetriever(_build_chroma(tmp_path / "chroma"), top_k=2)
    cases = [
        case("图书馆开放时间", "library.md"),
        case("宿舍禁止使用什么电器", "campus_rules.md"),
    ]

    report = evaluate_dataset(cases, retriever=retriever, top_k=2)

    assert report.total_cases == 2
    assert report.retrieved_cases == 2
    # 两个问题各自最相关的文档都排在 Top 1
    assert report.hit_at_1 == pytest.approx(1.0)
    assert report.cases[0].actual_sources[0] == "library.md"
    assert report.cases[1].actual_sources[0] == "campus_rules.md"
    assert all(score is not None for result in report.cases for score in result.scores)
    assert report.failed_cases == ()


def test_evaluate_dataset_on_empty_chroma_reports_zero_recall(tmp_path: Path) -> None:
    """空向量库上评估不抛异常，指标如实为 0，提示知识库尚未索引。"""

    empty = Chroma(
        collection_name="empty_evaluation",
        embedding_function=HashingEmbeddings(),
        persist_directory=str(tmp_path / "empty"),
        collection_metadata={"hnsw:space": "cosine"},
    )

    report = evaluate_dataset(
        [case("图书馆开放时间", "library.md")],
        retriever=KnowledgeRetriever(empty, top_k=3),
    )

    assert report.retrieved_cases == 0
    assert report.hit_at_k == pytest.approx(0.0)
    assert report.cases[0].precision == 0.0


# --------------------------- 3. 阈值 0.4 生效后的「负样本误召回」回归
#
# 背景：本地评估曾暴露负样本被稳定误召回 3/3 —— 那些片段的相关性落在
# 0.176–0.35，全都低于当前正式阈值 0.4。本组用例把「阈值开启后误召回归零」
# 这件事固化成回归，同时用对照组证明它确实来自阈值而不是巧合。

#: 项目当前正式启用的阈值（与 ``.env`` / ``.env.example`` 一致）。
CURRENT_THRESHOLD = 0.4

#: 负样本在被误召回时的真实相关性区间（0.35 / 0.25 / 0.176），均 < 0.4。
NEGATIVE_DISTANCES = (0.65, 0.75, 0.824)


@pytest.fixture
def threshold_store() -> ScriptedVectorStore:
    """正样本：1 条高相关（0.60）+ 1 条低相关（0.35）；负样本：只召回低相关片段。"""

    return ScriptedVectorStore(
        {
            "图书馆开放时间是怎么规定的？": [
                (make_document("图书馆开放时间 8:00-22:00。", source="campus_rules.md"), 0.40),
                (make_document("快递服务中心营业时间。", source="campus_faq.txt"), 0.65),
            ],
            "今天北京的天气怎么样？": [
                (make_document(f"与问题无关的片段 {index}。", source=f"noise{index}.md"), distance)
                for index, distance in enumerate(NEGATIVE_DISTANCES)
            ],
        }
    )


def _threshold_cases() -> list[EvaluationCase]:
    """一条正样本 + 一条负样本，与最小规模 LangSmith 评估同形。"""

    return [
        case("图书馆开放时间是怎么规定的？", "campus_rules.md"),
        case("今天北京的天气怎么样？"),
    ]


def _retriever_for(
    store: ScriptedVectorStore, *, score_threshold: float | None
) -> KnowledgeRetriever:
    return KnowledgeRetriever(store, top_k=DEFAULT_HIT_K, score_threshold=score_threshold)


def _negative_result(report: object) -> object:
    """取出负样本那条明细（不依赖用例顺序）。"""

    return next(result for result in report.cases if result.is_negative)  # type: ignore[attr-defined]


def test_current_threshold_suppresses_negative_unexpected_hits(
    threshold_store: ScriptedVectorStore,
) -> None:
    """阈值 0.4 生效后，负样本的低相关召回被拒，``unexpected_hits`` 归零。"""

    report = evaluate_dataset(
        _threshold_cases(),
        retriever=_retriever_for(threshold_store, score_threshold=CURRENT_THRESHOLD),
    )

    assert report.unexpected_hits == 0
    negative = _negative_result(report)
    assert negative.actual_sources == ()  # type: ignore[attr-defined]
    assert negative.retrieved == 0  # type: ignore[attr-defined]
    assert negative.hit_at_k is False  # type: ignore[attr-defined]


def test_without_threshold_the_same_negative_case_is_falsely_recalled(
    threshold_store: ScriptedVectorStore,
) -> None:
    """对照组：同一批数据在阈值关闭时确实会被误召回（说明上一条来自阈值）。"""

    report = evaluate_dataset(
        _threshold_cases(),
        retriever=_retriever_for(threshold_store, score_threshold=None),
    )

    assert report.unexpected_hits == 1
    assert _negative_result(report).actual_sources == (
        "noise0.md",
        "noise1.md",
        "noise2.md",
    )


def test_current_threshold_raises_precision_without_hurting_recall(
    threshold_store: ScriptedVectorStore,
) -> None:
    """阈值只做「拒绝低相关」：precision 提升，hit / recall 不受影响。"""

    cases = _threshold_cases()
    without = evaluate_dataset(cases, retriever=_retriever_for(threshold_store, score_threshold=None))
    with_threshold = evaluate_dataset(
        cases,
        retriever=_retriever_for(threshold_store, score_threshold=CURRENT_THRESHOLD),
    )

    # 命中类指标一个都不能退步
    assert with_threshold.hit_at_1 == without.hit_at_1 == pytest.approx(1.0)
    assert with_threshold.hit_at_k == without.hit_at_k == pytest.approx(1.0)
    assert with_threshold.recall == without.recall == pytest.approx(1.0)
    # 低相关片段被拒后 precision 从 1/2 升到 1/1
    assert without.precision == pytest.approx(0.5)
    assert with_threshold.precision == pytest.approx(1.0)
    # 汇总样本数不变：正样本仍计入，负样本仍不参与均值
    assert with_threshold.scored_cases == without.scored_cases == 1
    assert with_threshold.no_expected_source_cases == 1
