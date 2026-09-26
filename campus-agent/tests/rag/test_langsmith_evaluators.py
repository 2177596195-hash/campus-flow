"""把本地检索指标接入 LangSmith Evaluation 的测试（完全离线）。

被测的是**薄适配层**：``LangSmith Run + Example -> 复用本地指标 -> {key, score, comment}``。
因此这里验证的七组事：

==============================  ================================================
要验证的事                       对应用例
==============================  ================================================
提取（Run -> 检索结果）           sources / score / retrieved 的读取；空召回是
                                合法结果；Run 报错或字段缺失是「无法判定」
转换（Example -> 本地用例）       inputs / outputs / metadata 的还原；缺 question
                                时明确返回「无法判定」而不是猜
五个指标的口径                   hit@1 / hit@k / precision / recall / 负样本，
                                与本地 ``evaluator.score_case`` 逐项一致
输出契约与取值区间               ``{key, score, comment}``；score 恒在 ``[0, 1]``；
                                comment 带诊断信息；五者是独立 evaluator，无总分
目标函数与最小执行               target 走真实 Retriever；smoke=1 正 1 负；
                                全量需显式指定；LangSmith 客户端可用替身注入
回答相关性（LLM-as-a-Judge）      最终回答的提取（两种 Run 形状）、Judge 的结构化
                                输出与失败降级、``rag_answer_relevance`` 的
                                分数/理由/取值区间、缺回答记 None 而不是 0、
                                与检索实验互不混合
架构与旁路影响                   不碰 tracing（不写环境变量 / 无 tracing API）、
                                SDK 不在模块级 import、普通对话与 RAG 回答不变
==============================  ================================================

**全程不碰网络**：Run / Example 用 ``SimpleNamespace`` 造，LangSmith 客户端用
``FakeEvaluationClient``（继承 ``FakeDatasetClient`` 并补一个在本地同步执行
target + evaluators 的 ``evaluate``），检索器用 ``ScriptedVectorStore``，
回答相关性判定用 ``FakeJudge`` / ``StubLLM``（都不调模型）。
唯一会 import 官方 SDK 的路径在 ``langsmith_dataset.build_dataset_client()``，
本文件里连它也被 monkeypatch 换掉，因此没有任何用例会连上 LangSmith。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import main as cli
from app.config import settings as settings_module
from app.config.settings import ConfigurationError, get_langsmith_settings
from app.rag.evaluation import langsmith_evaluators as evaluators_module
from app.rag.evaluation.answer_judge import (
    JUDGE_TEMPERATURE,
    SCORE_MAX,
    SCORE_MIN,
    AnswerRelevanceJudge,
    AnswerRelevanceJudgement,
    AnswerRelevanceVerdict,
    to_judgement,
)
from app.rag.evaluation.langsmith_evaluators import (
    ANSWER_EVALUATOR_KEYS,
    ANSWER_EXPERIMENT_PREFIX,
    ANSWER_EXPERIMENT_TITLE,
    ANSWER_RELEVANCE_KEY,
    ANSWER_SELECTION,
    ANSWER_VERIFICATION_CASE_IDS,
    DEFAULT_EVALUATORS,
    DEFAULT_EVALUATOR_KEYS,
    EVALUATION_K,
    FULL_SELECTION,
    HIT_AT_1_KEY,
    HIT_AT_K_KEY,
    MISSING_ANSWER_REASON,
    NEGATIVE_SAMPLE_KEY,
    PRECISION_KEY,
    RECALL_KEY,
    SMOKE_SELECTION,
    build_answer_relevance_evaluator,
    build_answer_target,
    build_case,
    build_retrieval_target,
    extract_run_answer,
    extract_run_retrieval,
    judge,
    rag_hit_at_1,
    rag_hit_at_k,
    rag_negative_sample,
    rag_precision,
    rag_recall,
    run_answer_relevance_evaluation,
    run_retrieval_evaluation,
    select_examples,
    summarize_experiment,
)
from app.rag.evaluation.schema import DEFAULT_HIT_K
from app.rag.retriever import KnowledgeRetriever
from tests._fakes import (
    FakeDatasetClient,
    FakeRAGService,
    ScriptedChatModel,
    ScriptedVectorStore,
    StaticIntentClassifier,
    make_document,
)

#: 适配层的源码路径。与两条架构守卫里的豁免路径必须逐段一致，
#: 因此按「相对 app 根目录」的写法断言，路径一改就会当场失败。
_EXPECTED_RELATIVE_PATH: tuple[str, ...] = ("app", "rag", "evaluation", "langsmith_evaluators.py")

#: tracing 专有 API：适配层里出现任何一个，都说明它在借豁免偷偷加第二套追踪机制。
_TRACING_ONLY_TOKENS: tuple[str, ...] = (
    "@traceable",
    "traceable",
    "run_trees",
    "LangChainTracer",
    "tracing_is_enabled",
    "tracing_v2_enabled",
    "TRACING_V2",
    "add_runs",
)

#: 「本阶段不该被写」的追踪环境变量（conftest 会话级已把开关置为 false）。
_TRACING_ENV_KEYS: tuple[str, ...] = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_API_KEY",
)

#: 测试用的假密钥。**不是**真实凭证，只用于断言「不外泄」。
FAKE_API_KEY = "lsv2-eval-test-key-not-a-real-credential"


# ------------------------------------------------------------------ 构造与夹具


def make_example(
    question: str,
    *expected: str,
    keywords: Sequence[str] = (),
    min_score: float | None = None,
    case_id: str = "",
) -> SimpleNamespace:
    """构造一条 LangSmith Example 替身（字段名与官方 ``Example`` 对齐）。"""

    return SimpleNamespace(
        inputs={"question": question},
        outputs={"expected_sources": list(expected)},
        metadata={
            "case_id": case_id,
            "expected_keywords": list(keywords),
            "expected_min_score": min_score,
        },
    )


def make_run(
    *entries: tuple[str, float | None],
    retrieved: int | None = None,
    error: str | None = None,
) -> SimpleNamespace:
    """构造一次 RAG Run 替身。

    ``entries`` 是 ``(source, score)`` 序列，顺序即召回顺序（已在检索层排好）。
    ``sources`` 里刻意带上 ``title`` / ``snippet``，与生产 ``as_source()`` 同形。
    """

    if error is not None:
        return SimpleNamespace(outputs=None, error=error)
    outputs: dict[str, Any] = {
        "sources": [
            {"source": source, "title": source, "score": score, "snippet": f"{source} 的正文"}
            for source, score in entries
        ]
    }
    if retrieved is not None:
        outputs["retrieved"] = retrieved
    return SimpleNamespace(outputs=outputs, error=None)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离环境：不读本机 ``.env``、清空 LangSmith 配置、清掉配置缓存。"""

    monkeypatch.setattr(settings_module, "load_environment", lambda: None)
    for key in (*_TRACING_ENV_KEYS, "LANGSMITH_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    get_langsmith_settings.cache_clear()


class FakeExperimentResults:
    """官方 ``ExperimentResults`` 的替身：**可迭代**，并带 ``experiment_name``。

    这两点必须与官方一致——``summarize_experiment`` 就是「迭代结果行 + 读出实验名」，
    若替身只是个不可迭代的对象，真实代码路径根本没被走到。
    """

    def __init__(self, rows: Sequence[Any], *, experiment_name: str) -> None:
        self.rows = list(rows)
        self.experiment_name = experiment_name

    def __iter__(self) -> Any:
        return iter(self.rows)


class FakeEvaluationClient(FakeDatasetClient):
    """LangSmith 客户端的 **Dataset + Evaluation** 替身（不建连、不发请求）。

    在 ``FakeDatasetClient`` 之上补一个 ``evaluate``：它在本进程内按官方 SDK 的
    语义跑一遍——对每条 Example 调 target 拿到输出、把输出包成 Run、再依次调用
    evaluator——从而让 ``run_retrieval_evaluation`` 的整条编排（读数据集、挑样本、
    提交 evaluator、汇总结果）都被真实执行，而只有「谁来跑」被替换掉。
    """

    def __init__(self) -> None:
        super().__init__()
        #: 依次收到的 ``evaluate`` 调用参数（用于断言提交了哪些 evaluator 与样本）
        self.evaluate_calls: list[dict[str, Any]] = []

    def evaluate(
        self,
        target: Any,
        *,
        data: Any,
        evaluators: Sequence[Any],
        experiment_prefix: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> FakeExperimentResults:
        self.evaluate_calls.append(
            {
                "data": list(data),
                "evaluators": list(evaluators),
                "experiment_prefix": experiment_prefix,
                "metadata": dict(metadata or {}),
            }
        )
        rows = []
        for example in data:
            outputs = target(example.inputs)
            run = SimpleNamespace(outputs=outputs, error=None)
            results = [evaluator(run, example) for evaluator in evaluators]
            rows.append(
                {
                    "example": example,
                    "run": run,
                    "evaluation_results": {"results": results},
                }
            )
        return FakeExperimentResults(rows, experiment_name=f"{experiment_prefix}-offline")


def seeded_client(**examples: Any) -> FakeEvaluationClient:
    """构造一个已建好数据集、并写入给定 Example 的替身客户端。"""

    client = FakeEvaluationClient()
    client.create_dataset(evaluators_module_dataset_name())
    for name, example in examples.items():
        client.seed_example(
            evaluators_module_dataset_name(),
            inputs=example.inputs,
            outputs=example.outputs,
            metadata=example.metadata,
        )
    return client


def evaluators_module_dataset_name() -> str:
    """数据集名直接取自同步模块，避免这里再抄一份字面量。"""

    from app.rag.evaluation.langsmith_dataset import DEFAULT_DATASET_NAME

    return DEFAULT_DATASET_NAME


def retriever_with(script: Mapping[str, Sequence[tuple[Any, float]]]) -> KnowledgeRetriever:
    """用「按查询分派」的替身向量库构建真实检索器。"""

    return KnowledgeRetriever(ScriptedVectorStore(script), top_k=EVALUATION_K)


# ------------------------------------------------ 回答相关性专用替身与构造


def make_explicit_answer_run(answer: str) -> SimpleNamespace:
    """评估 target 形状的 Run：回答在 ``outputs["answer"]``。"""

    return SimpleNamespace(outputs={"answer": answer, "sources": []}, error=None)


def make_production_answer_run(answer: str, *, with_tool_call: bool = False) -> SimpleNamespace:
    """**生产 root run** 形状的 Run：回答在 ``outputs["messages"]`` 的末条 AI 消息里。

    ``with_tool_call=True`` 会先插一条内容为空的工具调用 AI 消息——
    这正是真实 Tool Calling 轮次留下的形态，回退逻辑必须跳过它。
    """

    messages: list[Any] = [{"type": "human", "content": "图书馆开放时间？"}]
    if with_tool_call:
        messages.append(
            {"type": "ai", "content": "", "tool_calls": [{"name": "get_score", "args": {}}]}
        )
    messages.append({"type": "ai", "content": answer})
    return SimpleNamespace(
        outputs={"messages": messages, "sources": [], "intent": "knowledge", "error": None},
        error=None,
    )


class FakeJudge:
    """``AnswerJudge`` 的替身：按预设判定返回，**不调用任何模型**。

    用它才验证得了「评估器的取值与组织」，而不必掺进模型行为；
    模型行为本身由 ``StubLLM`` / ``ScriptedChatModel`` 的两组用例单独覆盖。
    """

    def __init__(
        self,
        judgement: AnswerRelevanceJudgement | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.judgement = (
            judgement
            if judgement is not None
            else AnswerRelevanceJudgement(score=0.9, reason="直接回答了问题")
        )
        self.error = error
        #: 依次收到的 ``(问题, 回答)``——用于断言「只把问题与回答交给 Judge」
        self.calls: list[tuple[str, str]] = []

    def judge(self, question: str, answer: str) -> AnswerRelevanceJudgement:
        self.calls.append((question, answer))
        if self.error is not None:
            raise self.error
        return self.judgement


class _StubRunnable:
    """``with_structured_output`` 返回的可调用对象替身（只记录调用次数）。"""

    def __init__(self, llm: StubLLM) -> None:
        self._llm = llm

    def invoke(self, messages: Any) -> Any:
        self._llm.invoke_count += 1
        if self._llm.invoke_error is not None:
            raise self._llm.invoke_error
        return self._llm.result


class StubLLM:
    """``with_structured_output`` 的最小替身：记录绑定/调用次数，返回固定结果。

    用它才观察得到「结构化输出只绑定一次」「绑定失败即永久不可用」
    「单次调用失败不永久降级」这三条契约——真实模型对象上这些没有可读痕迹。
    """

    def __init__(
        self,
        *,
        result: Any = None,
        bind_error: BaseException | None = None,
        invoke_error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.bind_error = bind_error
        self.invoke_error = invoke_error
        self.bind_count = 0
        self.invoke_count = 0
        #: 依次收到的 ``method=`` 参数，用于断言用的是 function_calling
        self.methods: list[Any] = []

    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kwargs: Any
    ) -> Any:
        self.bind_count += 1
        self.methods.append(method)
        if self.bind_error is not None:
            raise self.bind_error
        return _StubRunnable(self)


def verdict_tool_call(score: Any, reason: str = "") -> AIMessage:
    """构造一次「模型调用了评测函数」的回复（真实 LangChain 工具调用契约）。

    函数名取 pydantic 模型名——这正是 ``with_structured_output(method="function_calling")``
    生成的 OpenAI 工具名，已用真实对象核对过，不是猜的。
    """

    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": AnswerRelevanceVerdict.__name__,
                "args": {"score": score, "reason": reason},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )


# =============================================== 1. 提取：Run -> 检索结果


def test_extracts_sources_scores_and_count_from_run() -> None:
    """来源、分数与条数都能从 Run 输出里读出来（分数保持数值语义）。"""

    retrieval = extract_run_retrieval(
        make_run(("campus_rules.md", 0.91), ("campus_faq.txt", 0.42))
    )

    assert retrieval.unreadable is None
    assert retrieval.sources == ("campus_rules.md", "campus_faq.txt")
    assert retrieval.chunks[0].score == pytest.approx(0.91)
    assert retrieval.retrieved == 2


def test_retrieved_falls_back_to_source_count_when_absent() -> None:
    """真实 Agent 的 knowledge 节点不写 ``retrieved``：用来源条数补齐，不当成缺失。"""

    retrieval = extract_run_retrieval(make_run(("a.md", 0.5), ("b.md", 0.4)))

    assert retrieval.retrieved == 2


def test_explicit_retrieved_count_wins() -> None:
    """Run 里显式给了条数时以它为准（条数与去重后的来源数可以不同）。"""

    retrieval = extract_run_retrieval(make_run(("a.md", 0.5), retrieved=5))

    assert retrieval.retrieved == 5


def test_empty_source_list_is_a_legal_zero_recall() -> None:
    """空数组是**合法的零召回**，不是错误：这正是负样本「什么都没召回」的形态。"""

    retrieval = extract_run_retrieval(make_run())

    assert retrieval.unreadable is None
    assert retrieval.chunks == ()
    assert retrieval.sources == ()
    assert retrieval.retrieved == 0


def test_failed_run_is_unreadable_not_zero_recall() -> None:
    """Run 报错时**不能**当成零召回：那会把基础设施故障伪装成检索质量问题。"""

    retrieval = extract_run_retrieval(make_run(error="RuntimeError: boom"))

    assert retrieval.unreadable is not None
    assert "boom" in retrieval.unreadable


@pytest.mark.parametrize(
    ("outputs", "matcher"),
    [
        (None, "输出为空"),
        ({}, "缺少 sources"),
        ({"sources": "campus_rules.md"}, "不是数组"),
        ({"sources": ["campus_rules.md"]}, "非法条目"),
        ({"sources": [42]}, "非法条目"),
    ],
)
def test_structurally_invalid_outputs_are_unreadable(outputs: Any, matcher: str) -> None:
    """结构非法一律判为「无法判定」，并说清到底哪里不对。"""

    retrieval = extract_run_retrieval(SimpleNamespace(outputs=outputs, error=None))

    assert retrieval.unreadable is not None
    assert matcher in retrieval.unreadable


def test_entry_without_a_source_is_treated_as_an_unknown_source() -> None:
    """条目缺少 ``source``：按本地口径归一为「未知来源」，既不丢弃也不误判成命中。

    这与 ``evaluator`` 的处理一致（``chunk.source or 未知来源``）——一个来源不明的
    片段不该被当成「命中期望来源」，但也不该让整次 Run 变成读不出来。
    """

    outputs = {"sources": [{"title": "无来源的片段"}]}

    retrieval = extract_run_retrieval(SimpleNamespace(outputs=outputs, error=None))
    assert retrieval.unreadable is None
    assert retrieval.chunks[0].source == ""

    output = rag_precision(
        SimpleNamespace(outputs=outputs, error=None),
        make_example("图书馆开放时间？", "campus_rules.md"),
    )
    assert output["score"] == 0.0
    assert "未知来源" in output["comment"]


def test_boolean_score_is_treated_as_unknown() -> None:
    """``True`` 是 ``bool`` 不是分数：按「分数未知」处理，绝不参与比较。"""

    retrieval = extract_run_retrieval(make_run(("a.md", True)))

    assert retrieval.chunks[0].score is None


# =============================================== 2. 转换：Example -> 本地用例


def test_build_case_restores_local_fields() -> None:
    """问题、期望来源与 metadata 被还原成本地 ``EvaluationCase``。"""

    case = build_case(
        make_example(
            "图书馆开放时间？",
            "campus_rules.md",
            keywords=("开放时间",),
            min_score=0.5,
            case_id="library-hours",
        )
    )

    assert case is not None
    assert case.question == "图书馆开放时间？"
    assert case.expected_sources == ("campus_rules.md",)
    assert case.expected_keywords == ("开放时间",)
    assert case.expected_min_score == 0.5
    assert case.case_id == "library-hours"


def test_build_case_without_metadata_still_judges_hits() -> None:
    """metadata 是可选增强：没有它也能判 hit / precision / recall。"""

    example = SimpleNamespace(
        inputs={"question": "图书馆开放时间？"},
        outputs={"expected_sources": ["campus_rules.md"]},
    )

    case = build_case(example)

    assert case is not None
    assert case.expected_sources == ("campus_rules.md",)
    assert case.case_id == ""
    assert case.expected_min_score is None


@pytest.mark.parametrize("question", ["", "   ", None, 123])
def test_build_case_without_a_question_returns_none(question: Any) -> None:
    """没有可用的问题就真的没法判定——返回 ``None``，而不是猜一个空查询去检索。"""

    example = SimpleNamespace(
        inputs={"question": question}, outputs={"expected_sources": ["a.md"]}
    )

    assert build_case(example) is None


def test_judge_reports_unavailable_when_example_has_no_question() -> None:
    """``judge`` 把「用例不可用」与「检索结果不可读」都表达为 ``unavailable``。"""

    judgement = judge(make_run(("a.md", 0.9)), SimpleNamespace(inputs={}, outputs={}))

    assert judgement.result is None
    assert judgement.unavailable is not None
    assert "question" in judgement.unavailable


# =============================================== 3. 五个指标的口径


def test_hit_at_1_scores_one_when_top1_is_expected() -> None:
    """Top 1 命中期望来源时 ``rag_hit_at_1`` 为 1.0。"""

    output = rag_hit_at_1(
        make_run(("campus_rules.md", 0.9), ("campus_faq.txt", 0.4)),
        make_example("图书馆开放时间？", "campus_rules.md"),
    )

    assert output["key"] == HIT_AT_1_KEY
    assert output["score"] == 1.0


def test_hit_at_1_scores_zero_when_expected_is_second() -> None:
    """期望来源排在第 2 位：hit@1 为 0，但 hit@k 仍为 1（两个指标互不代偿）。"""

    run = make_run(("campus_faq.txt", 0.9), ("campus_rules.md", 0.8))
    example = make_example("图书馆开放时间？", "campus_rules.md")

    assert rag_hit_at_1(run, example)["score"] == 0.0
    assert rag_hit_at_k(run, example)["score"] == 1.0


def test_hit_at_k_respects_the_window() -> None:
    """k 之外的命中不计入 hit@k：第 3 位算命中，第 4 位不算（k=3）。"""

    example = make_example("问题", "campus_rules.md")

    inside = make_run(
        ("a.md", 0.9), ("b.md", 0.8), ("campus_rules.md", 0.7)
    )
    outside = make_run(
        ("a.md", 0.9), ("b.md", 0.8), ("c.md", 0.7), ("campus_rules.md", 0.6)
    )

    assert rag_hit_at_k(inside, example)["score"] == 1.0
    assert rag_hit_at_k(outside, example)["score"] == 0.0


def test_precision_is_share_of_expected_among_returned() -> None:
    """precision = 返回来源中命中期望的比例（此处 2/3），且分片同源只算一个。"""

    output = rag_precision(
        make_run(
            ("campus_rules.md", 0.9),
            ("scholarship.md", 0.8),
            ("campus_faq.txt", 0.7),
        ),
        make_example("图书馆和奖学金？", "campus_rules.md", "scholarship.md"),
    )

    assert output["score"] == pytest.approx(2 / 3)


def test_duplicate_fragments_do_not_inflate_precision() -> None:
    """同一文档的多个片段只算一个来源（与本地口径一致）。"""

    output = rag_precision(
        make_run(("campus_rules.md", 0.9), ("campus_rules.md", 0.8), ("campus_rules.md", 0.7)),
        make_example("图书馆开放时间？", "campus_rules.md"),
    )

    assert output["score"] == pytest.approx(1.0)


def test_recall_is_partial_for_multiple_expected_sources() -> None:
    """expected_sources 有两个只召回其一：recall = 0.5。"""

    output = rag_recall(
        make_run(("campus_rules.md", 0.9), ("campus_faq.txt", 0.4)),
        make_example("图书馆和体育馆？", "campus_rules.md", "campus_sports.pdf"),
    )

    assert output["score"] == pytest.approx(0.5)


def test_zero_recall_scores_zero_not_undefined() -> None:
    """有期望来源却零召回：precision / recall 记为 0.0（与本地口径一致，不是未定义）。"""

    example = make_example("图书馆开放时间？", "campus_rules.md")
    run = make_run()

    assert rag_precision(run, example)["score"] == 0.0
    assert rag_recall(run, example)["score"] == 0.0
    assert rag_hit_at_1(run, example)["score"] == 0.0
    assert rag_hit_at_k(run, example)["score"] == 0.0


# --------------------------------------------------- 负样本（严格沿用本地口径）


def test_negative_sample_scores_one_when_nothing_is_retrieved() -> None:
    """负样本未召回任何片段：符合预期，``rag_negative_sample`` 为 1.0。"""

    output = rag_negative_sample(make_run(), make_example("今天天气怎么样？"))

    assert output["key"] == NEGATIVE_SAMPLE_KEY
    assert output["score"] == 1.0
    assert "符合预期" in output["comment"]


def test_negative_sample_scores_zero_and_names_the_unexpected_hits() -> None:
    """负样本被误召回：得 0 分，并且 comment 里能直接看出召回了什么。"""

    run = make_run(("campus_faq.txt", 0.8), ("campus_rules.md", 0.2))
    output = rag_negative_sample(run, make_example("今天天气怎么样？"))

    assert output["score"] == 0.0
    assert "误召回" in output["comment"]
    assert "campus_faq.txt" in output["comment"]


@pytest.mark.parametrize(
    "evaluator",
    [rag_hit_at_1, rag_hit_at_k, rag_precision, rag_recall],
)
def test_negative_metrics_are_undefined_rather_than_zero(evaluator: Any) -> None:
    """负样本上四个指标**未定义**（``None``），既不按 0 也不按 1 计入汇总。"""

    output = evaluator(make_run(("campus_faq.txt", 0.8)), make_example("今天天气怎么样？"))

    assert output["score"] is None
    assert "未定义" in output["comment"]


def test_negative_sample_metric_is_not_applicable_to_positive_cases() -> None:
    """``rag_negative_sample`` 只对负样本有意义；正样本上不给分。"""

    output = rag_negative_sample(
        make_run(("campus_rules.md", 0.9)), make_example("图书馆开放时间？", "campus_rules.md")
    )

    assert output["score"] is None
    assert "不适用" in output["comment"]


def test_empty_expected_sources_never_produce_a_fake_recall() -> None:
    """负样本即使命中了知识库，也**不会**被算成 recall=1（那是把口径反过来了）。"""

    run = make_run(("campus_rules.md", 0.9))

    assert rag_recall(run, make_example("今天天气怎么样？"))["score"] is None
    assert rag_precision(run, make_example("今天天气怎么样？"))["score"] is None


# =============================================== 4. 输出契约与诊断


def test_every_evaluator_returns_key_score_comment() -> None:
    """五个 evaluator 的输出恒为 ``{key, score, comment}``，key 与约定一致。"""

    run = make_run(("campus_rules.md", 0.9))
    example = make_example("图书馆开放时间？", "campus_rules.md")

    keys = []
    for evaluator in DEFAULT_EVALUATORS:
        output = evaluator(run, example)
        assert set(output) == {"key", "score", "comment"}
        assert isinstance(output["key"], str)
        assert isinstance(output["comment"], str) and output["comment"]
        keys.append(output["key"])

    assert keys == list(DEFAULT_EVALUATOR_KEYS)
    assert len(set(keys)) == len(keys) == 5


def test_scores_stay_within_the_unit_interval() -> None:
    """所有分数（或未定义时的 ``None``）都落在 ``[0, 1]`` 内。"""

    scenarios = [
        (make_run(), make_example("今天天气怎么样？")),
        (make_run(("a.md", 0.9)), make_example("今天天气怎么样？")),
        (make_run(("campus_rules.md", 0.9), ("b.md", 0.5)), make_example("问", "campus_rules.md")),
        (make_run(("x.md", 0.5), ("y.md", 0.4)), make_example("问", "a.md", "b.md")),
    ]

    for run, example in scenarios:
        for evaluator in DEFAULT_EVALUATORS:
            score = evaluator(run, example)["score"]
            assert score is None or 0.0 <= score <= 1.0, (evaluator.__name__, score)


def test_comment_carries_actionable_diagnostics() -> None:
    """comment 里能直接看到期望、实际（含分数）、排名与漏召来源。"""

    output = rag_recall(
        make_run(("campus_rules.md", 0.9001), ("campus_faq.txt", 0.4213)),
        make_example("图书馆和体育馆？", "campus_rules.md", "campus_sports.pdf"),
    )

    comment = output["comment"]
    assert "expected=[campus_rules.md, campus_sports.pdf]" in comment
    assert "campus_rules.md(0.900)" in comment
    assert "first_relevant_rank=1" in comment
    assert "missed=[campus_sports.pdf]" in comment


def test_score_unknown_is_rendered_as_a_dash() -> None:
    """分数未知的召回来源在诊断里显示 ``-``，不会被编造成一个数字。"""

    output = rag_hit_at_1(make_run(("a.md", None)), make_example("问", "a.md"))

    assert "a.md(-)" in output["comment"]


def test_diagnostics_note_a_regressed_score_calibration() -> None:
    """用例声明了分数下限且未达到时，comment 给出标定告警（不影响命中判定）。"""

    output = rag_hit_at_k(
        make_run(("campus_rules.md", 0.8)),
        make_example("图书馆开放时间？", "campus_rules.md", min_score=0.95),
    )

    assert output["score"] == 1.0  # 命中判定不受标定影响
    assert "分数标定" in output["comment"]


def test_comment_never_uses_truncated_snippet_keyword_stats() -> None:
    """comment 不出现关键词统计：Run 里的 ``snippet`` 被截断，统计会误导。"""

    output = rag_hit_at_k(
        make_run(("campus_rules.md", 0.9)),
        make_example("图书馆开放时间？", "campus_rules.md", keywords=("开放时间",)),
    )

    assert "keywords" not in output["comment"]
    assert "开放时间" not in output["comment"]


# =============================================== 5. Run 字段缺失时的行为


@pytest.mark.parametrize("evaluator", DEFAULT_EVALUATORS)
def test_missing_run_output_gives_none_score_and_a_clear_reason(evaluator: Any) -> None:
    """Run 输出缺失时每个指标都返回 ``score=None`` 与明确原因（而不是崩溃或假 0）。"""

    run = SimpleNamespace(outputs=None, error=None)
    output = evaluator(run, make_example("图书馆开放时间？", "campus_rules.md"))

    assert output["score"] is None
    assert output["comment"].startswith("无法判定")
    assert "输出为空" in output["comment"]


@pytest.mark.parametrize("evaluator", DEFAULT_EVALUATORS)
def test_failed_run_does_not_look_like_a_retrieval_failure(evaluator: Any) -> None:
    """Run 执行失败时同样「无法判定」，并且原因里保留底层错误信息。"""

    output = evaluator(make_run(error="TimeoutError: upstream"), make_example("问", "a.md"))

    assert output["score"] is None
    assert "TimeoutError" in output["comment"]


def test_missing_source_field_lists_what_was_actually_present() -> None:
    """缺 ``sources`` 键时把实际拿到的键列出来，便于定位上游改了什么。"""

    output = rag_hit_at_k(
        SimpleNamespace(outputs={"answer": "……"}, error=None), make_example("问", "a.md")
    )

    assert output["score"] is None
    assert "answer" in output["comment"]


# =============================================== 6. 目标函数与最小执行


def test_target_returns_sources_in_the_production_shape() -> None:
    """target 的输出与 knowledge 节点写进状态的 ``sources`` 同形（含分数与片段）。"""

    retriever = retriever_with(
        {
            "图书馆开放时间？": [
                (make_document("图书馆 8:00-22:00。", source="campus_rules.md"), 0.10),
                (make_document("快递中心营业时间。", source="campus_faq.txt"), 0.60),
            ]
        }
    )
    target = build_retrieval_target(retriever=retriever)

    outputs = target({"question": "图书馆开放时间？"})

    assert outputs["retrieved"] == 2
    assert [item["source"] for item in outputs["sources"]] == [
        "campus_rules.md",
        "campus_faq.txt",
    ]
    assert set(outputs["sources"][0]) == {"source", "title", "score", "snippet"}
    assert outputs["sources"][0]["score"] == pytest.approx(0.9)
    # target 的输出可以直接喂给适配层——这正是「同一套形状」的意义
    assert extract_run_retrieval(SimpleNamespace(outputs=outputs, error=None)).unreadable is None


def test_target_forwards_the_k_window_to_the_retriever() -> None:
    """``top_k`` 被透传到检索器：k=1 时只应拿到一条。"""

    script = {
        "问题": [
            (make_document("正文一", source="a.md"), 0.1),
            (make_document("正文二", source="b.md"), 0.2),
            (make_document("正文三", source="c.md"), 0.3),
        ]
    }
    store = ScriptedVectorStore(script)
    retriever = KnowledgeRetriever(store, top_k=EVALUATION_K)

    outputs = build_retrieval_target(retriever=retriever, top_k=1)({"question": "问题"})

    assert outputs["retrieved"] == 1
    assert [item["source"] for item in outputs["sources"]] == ["a.md"]


def test_target_builds_the_default_retriever_lazily_and_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认检索器惰性构建且整个实验复用：两次提问只建一次连接。"""

    created: list[bool] = []
    store = ScriptedVectorStore({"问题": [(make_document("正文", source="a.md"), 0.1)]})

    def factory() -> KnowledgeRetriever:
        created.append(True)
        return KnowledgeRetriever(store, top_k=EVALUATION_K)

    monkeypatch.setattr(evaluators_module, "default_retriever", factory)
    target = build_retrieval_target()

    assert created == [], "构建 target 时不该立即建立连接"
    target({"question": "问题"})
    target({"question": "问题"})

    assert len(created) == 1
    assert store.queries == ["问题", "问题"]


def test_target_does_not_retrieve_when_the_question_is_missing() -> None:
    """缺问题时先抛错，一次检索都不该发生（避免白白打向量库）。"""

    store = ScriptedVectorStore({"问题": [(make_document("正文", source="a.md"), 0.1)]})
    target = build_retrieval_target(retriever=KnowledgeRetriever(store))

    with pytest.raises(ValueError, match="question"):
        target({"question": "   "})

    assert store.queries == []


def test_smoke_selection_picks_one_positive_and_one_negative() -> None:
    """最小样本 = 1 正 + 1 负：既能验证命中，也能验证负样本被判为「未定义」。"""

    examples = [
        make_example("正一", "a.md"),
        make_example("正二", "b.md"),
        make_example("负一"),
        make_example("负二"),
    ]

    picked = select_examples(examples, selection=SMOKE_SELECTION)

    assert [item.inputs["question"] for item in picked] == ["正一", "负一"]


def test_full_selection_returns_everything() -> None:
    """全量口径不做任何挑选。"""

    examples = [make_example("正一", "a.md"), make_example("负一")]

    assert select_examples(examples, selection=FULL_SELECTION) == examples


def test_smoke_selection_rejects_an_empty_dataset() -> None:
    """数据集为空就没得跑：直接报错，而不是提交一个空实验。"""

    with pytest.raises(ValueError, match="没有可用"):
        select_examples([], selection=SMOKE_SELECTION)


def test_minimal_evaluation_runs_end_to_end_offline() -> None:
    """整条编排：读数据集 → 挑 1 正 1 负 → 跑真实检索 → 五个指标分别汇总。"""

    client = seeded_client(
        positive=make_example("图书馆开放时间？", "campus_rules.md"),
        negative=make_example("今天天气怎么样？"),
    )
    retriever = retriever_with(
        {
            "图书馆开放时间？": [
                (make_document("图书馆 8:00-22:00。", source="campus_rules.md"), 0.10),
                (make_document("快递中心营业时间。", source="campus_faq.txt"), 0.60),
            ]
        }
    )

    summary = run_retrieval_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_retrieval_target(retriever=retriever),
        selection=SMOKE_SELECTION,
    )

    # 提交给官方 SDK 的只有挑中的 2 条与 5 个独立 evaluator
    call = client.evaluate_calls[0]
    assert len(call["data"]) == 2
    assert call["evaluators"] == list(DEFAULT_EVALUATORS)
    assert [fn.__name__ for fn in call["evaluators"]] == [
        "rag_hit_at_1",
        "rag_hit_at_k",
        "rag_precision",
        "rag_recall",
        "rag_negative_sample",
    ]
    assert call["metadata"]["selection"] == SMOKE_SELECTION
    assert call["metadata"]["k"] == EVALUATION_K

    assert summary.cases == 2
    assert summary.positives == 1
    assert summary.negatives == 1
    assert summary.experiment_name == "campus-flow-rag-retrieval-offline"
    assert summary.scores[HIT_AT_1_KEY] == (1.0,)
    assert summary.scores[HIT_AT_K_KEY] == (1.0,)
    assert summary.scores[PRECISION_KEY] == (pytest.approx(0.5),)
    assert summary.scores[RECALL_KEY] == (1.0,)
    assert summary.scores[NEGATIVE_SAMPLE_KEY] == (1.0,)


def test_minimal_evaluation_marks_a_false_positive_negative() -> None:
    """负样本被误召回时，``rag_negative_sample`` 得 0——这正是要观测的回归信号。"""

    client = seeded_client(negative=make_example("今天天气怎么样？"))
    retriever = retriever_with(
        {
            "今天天气怎么样？": [
                (make_document("校园卡挂失流程。", source="campus_faq.txt"), 0.20),
            ]
        }
    )

    summary = run_retrieval_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_retrieval_target(retriever=retriever),
        selection=FULL_SELECTION,
    )

    assert summary.negatives == 1
    assert summary.scores[NEGATIVE_SAMPLE_KEY] == (0.0,)
    # 负样本不参与其余四项：它们根本不该出现在分数集合里
    for key in (HIT_AT_1_KEY, HIT_AT_K_KEY, PRECISION_KEY, RECALL_KEY):
        assert summary.scores.get(key, ()) == ()


def test_summarize_experiment_skips_undefined_scores() -> None:
    """``score=None``（未定义 / 无法判定）的条目不进分数集合，均值才不失真。"""

    rows = [
        {
            "example": make_example("正一", "a.md"),
            "evaluation_results": {
                "results": [
                    {"key": HIT_AT_1_KEY, "score": 1.0, "comment": ""},
                    {"key": RECALL_KEY, "score": None, "comment": "负样本"},
                    {"key": PRECISION_KEY, "score": "high", "comment": "非法值"},
                ]
            },
        },
        {
            "example": make_example("正二", "b.md"),
            "evaluation_results": {
                "results": [{"key": HIT_AT_1_KEY, "score": 0.0, "comment": ""}]
            },
        },
    ]

    summary = summarize_experiment(
        rows, dataset_name="ds", experiment_name="exp"
    )

    assert summary.cases == 2
    assert summary.positives == 2
    assert summary.negatives == 0
    assert summary.scores[HIT_AT_1_KEY] == (1.0, 0.0)
    assert RECALL_KEY not in summary.scores
    assert PRECISION_KEY not in summary.scores


def test_report_lists_every_metric_line() -> None:
    """CLI 报告必须逐项列出五个指标（无有效分数时如实标注，而不是藏起来）。"""

    client = seeded_client(positive=make_example("图书馆开放时间？", "campus_rules.md"))
    retriever = retriever_with(
        {"图书馆开放时间？": [(make_document("正文", source="campus_rules.md"), 0.10)]}
    )

    summary = run_retrieval_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_retrieval_target(retriever=retriever),
    )
    report = summary.format_report()

    assert report.startswith("LangSmith RAG Evaluation\n")
    assert f"Dataset: {evaluators_module_dataset_name()}" in report
    for key in DEFAULT_EVALUATOR_KEYS:
        assert key in report


# =============================================== 7. CLI 接线


def test_langsmith_evaluation_flag_defaults_to_off() -> None:
    """默认不评估：只有显式参数才会触发。"""

    assert cli._parse_args([]).evaluate_rag_langsmith is False
    assert cli._parse_args(["你好"]).evaluate_rag_langsmith is False
    assert cli._parse_args(["--evaluate-rag-langsmith"]).evaluate_rag_langsmith is True
    assert cli._parse_args(["--langsmith-full"]).langsmith_full is True


def test_langsmith_full_requires_the_evaluation_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--langsmith-full`` 单独出现是参数错误：它只在评估入口里有意义。"""

    _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["--langsmith-full"]) == 2
    assert "--evaluate-rag-langsmith" in capsys.readouterr().err


def test_normal_run_never_touches_the_langsmith_evaluation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """普通一轮对话照旧：不建客户端、不跑评估。"""

    monkeypatch.setattr(
        cli, "build_dataset_client", lambda: pytest.fail("普通对话不该碰 LangSmith 评估")
    )
    monkeypatch.setattr(
        cli, "run_retrieval_evaluation", lambda **kwargs: pytest.fail("普通对话不该跑评估")
    )

    class FakeRuntime:
        def invoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
            return {"messages": [AIMessage(content="你好。")], "intent": "chat"}

    assert cli._run_once(FakeRuntime(), "你好", "session-1") == 0
    assert "你好。" in capsys.readouterr().out


def test_main_never_evaluates_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python main.py "xxx"`` 这条路径上不存在任何评估动作。"""

    observed = _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["你好"]) == 0
    assert observed["langsmith"] == []
    assert observed["runtime"] == [True], "普通提问仍要装配运行时"


def test_main_dispatches_the_evaluation_when_flag_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式传参时才走到评估分支，且**不**装配 Agent 运行时；默认是最小样本。"""

    observed = _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["--evaluate-rag-langsmith"]) == 0
    assert observed["langsmith"] == [False]
    assert observed["runtime"] == []

    assert cli.main(["--evaluate-rag-langsmith", "--langsmith-full"]) == 0
    assert observed["langsmith"] == [False, True]


def _patch_main_for_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """把 ``main()`` 的周围依赖换成轻薄替身，只观察「派发去了哪里」。"""

    observed: dict[str, list[Any]] = {"langsmith": [], "answer": [], "runtime": []}
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
        cli,
        "_run_evaluate_rag_langsmith",
        lambda *, full=False: (observed["langsmith"].append(full), 0)[1],
    )
    monkeypatch.setattr(
        cli,
        "_run_evaluate_rag_answer",
        lambda: (observed["answer"].append(True), 0)[1],
    )
    monkeypatch.setattr(cli, "_run_once", lambda *args: 0)
    return observed


def test_evaluation_entry_point_reports_a_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未配置 LangSmith 时给清晰提示并以配置错误码退出（不是崩溃）。"""

    exit_code = cli._run_evaluate_rag_langsmith()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "配置错误" in captured.err
    assert "LANGSMITH_API_KEY" in captured.err
    assert FAKE_API_KEY not in captured.err


def test_evaluation_entry_point_prints_the_experiment_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 报告给出数据集、实验名与五个指标；密钥不出现在输出里。"""

    client = seeded_client(
        positive=make_example("图书馆开放时间？", "campus_rules.md"),
        negative=make_example("今天天气怎么样？"),
    )
    retriever = retriever_with(
        {"图书馆开放时间？": [(make_document("正文", source="campus_rules.md"), 0.10)]}
    )

    monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
    get_langsmith_settings.cache_clear()
    monkeypatch.setattr(cli, "build_dataset_client", lambda: client)
    monkeypatch.setattr(cli, "default_retriever", lambda: retriever)

    assert cli._run_evaluate_rag_langsmith() == 0

    report = capsys.readouterr().out
    assert evaluators_module_dataset_name() in report
    assert "Experiment: " in report
    assert HIT_AT_1_KEY in report
    assert NEGATIVE_SAMPLE_KEY in report
    assert FAKE_API_KEY not in report


def test_evaluation_entry_point_forwards_the_full_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``full=True`` 时提交数据集全部用例（默认只提交最小样本）。"""

    client = seeded_client(
        positive=make_example("图书馆开放时间？", "campus_rules.md"),
        negative=make_example("今天天气怎么样？"),
    )
    retriever = retriever_with(
        {"图书馆开放时间？": [(make_document("正文", source="campus_rules.md"), 0.10)]}
    )
    monkeypatch.setattr(cli, "build_dataset_client", lambda: client)
    monkeypatch.setattr(cli, "default_retriever", lambda: retriever)

    assert cli._run_evaluate_rag_langsmith(full=True) == 0

    assert len(client.evaluate_calls[0]["data"]) == 2
    assert client.evaluate_calls[0]["metadata"]["selection"] == FULL_SELECTION


# =============================================== 8. 架构与旁路影响


def test_module_path_matches_the_guard_exemptions() -> None:
    """两条架构守卫按路径豁免本模块；路径写错等于豁免失效，且会让守卫误报。"""

    expected = Path(settings_module.app_root()).joinpath(*_EXPECTED_RELATIVE_PATH)

    assert Path(evaluators_module.__file__) == expected


def test_module_contains_no_tracing_api() -> None:
    """豁免只允许 Datasets / Evaluation API：出现 tracing 专有 API 即视为越界。"""

    source = Path(evaluators_module.__file__).read_text(encoding="utf-8")

    leaked = [token for token in _TRACING_ONLY_TOKENS if token in source]
    assert leaked == [], f"适配层出现了 tracing 专有 API: {leaked}"


def test_langsmith_sdk_is_not_imported_at_module_level() -> None:
    """SDK 与配置入口都不在模块级 import：导入本模块零副作用。"""

    source = Path(evaluators_module.__file__).read_text(encoding="utf-8")
    top_level_imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]

    assert not any("langsmith" in line for line in top_level_imports), top_level_imports
    assert not any(line.startswith("import langsmith") for line in top_level_imports)


def test_evaluation_module_reuses_the_local_metrics_instead_of_redefining_them() -> None:
    """指标口径只有一处实现：适配层调用 ``score_case``，不自己算 hit / precision。"""

    source = Path(evaluators_module.__file__).read_text(encoding="utf-8")

    assert "score_case" in source
    assert "from app.rag.evaluation.evaluator import" in source
    # 不得出现任何「自己实现指标」的痕迹
    for token in ("expected_set", "len(matched)", "len(expected)", "hit_at_1=", "hit_at_k="):
        assert token not in source, f"适配层似乎在自行重算指标: {token}"


def test_evaluation_module_never_touches_tracing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跑一遍全部指标：一个追踪相关的环境变量都不该被写。"""

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    before = {key: os.environ.get(key) for key in _TRACING_ENV_KEYS}

    run = make_run(("campus_rules.md", 0.9))
    example = make_example("图书馆开放时间？", "campus_rules.md")
    for evaluator in DEFAULT_EVALUATORS:
        evaluator(run, example)

    after = {key: os.environ.get(key) for key in _TRACING_ENV_KEYS}
    assert after == before


def test_rag_answer_and_sources_are_unchanged() -> None:
    """导入并使用适配层后，knowledge 分支的回答与引用来源一字不变。"""

    from app.agent.graph import build_graph

    rag_service = FakeRAGService(
        answer="图书馆开放时间为周一至周五 8:00–22:00。",
        sources=({"source": "campus_rules.md", "title": "校园规章", "score": 0.9},),
    )
    graph = build_graph(
        llm=ScriptedChatModel.scripted(),
        intent_classifier=StaticIntentClassifier("knowledge"),
        rag_service=rag_service,
        tools=(),
    )

    state = graph.invoke(
        {"messages": [HumanMessage(content="图书馆开放时间？")]},
        config={"configurable": {"thread_id": "eval-bypass"}},
    )

    assert state["messages"][-1].content == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert state["sources"] == [
        {"source": "campus_rules.md", "title": "校园规章", "score": 0.9}
    ]
    assert rag_service.calls and rag_service.calls[0][0] == "图书馆开放时间？"


def test_evaluation_k_matches_the_local_default_window() -> None:
    """评估窗口与本地 ``--evaluate-rag`` 对齐：两边的数字才能直接对看。"""

    assert EVALUATION_K == DEFAULT_HIT_K
    assert DEFAULT_EVALUATOR_KEYS == (
        HIT_AT_1_KEY,
        HIT_AT_K_KEY,
        PRECISION_KEY,
        RECALL_KEY,
        NEGATIVE_SAMPLE_KEY,
    )


def test_configuration_error_type_is_reused() -> None:
    """未配置时抛的是本项目统一的 ``ConfigurationError``，CLI 才能给出明确提示。"""

    assert issubclass(ConfigurationError, Exception)
    with pytest.raises(ConfigurationError):
        cli.build_dataset_client()


# =============================================== 9. 回答相关性（LLM-as-a-Judge）


# --------------------------------------------------- 9.1 提取最终回答


def test_extract_answer_prefers_the_explicit_answer_field() -> None:
    extraction = extract_run_answer(make_explicit_answer_run("图书馆 8:00-22:00。"))

    assert extraction.unreadable is None
    assert extraction.answer == "图书馆 8:00-22:00。"


def test_extract_answer_falls_back_to_the_production_messages_shape() -> None:
    """生产 root run 没有独立 answer 字段，回答在 messages 里——回退路径要能读到。"""

    extraction = extract_run_answer(make_production_answer_run("体育馆 6:00-21:00。"))

    assert extraction.unreadable is None
    assert extraction.answer == "体育馆 6:00-21:00。"


def test_extract_answer_skips_the_empty_tool_calling_ai_message() -> None:
    """工具调用那条 AI 消息内容为空：回退必须继续往前找，而不是把空串当回答。"""

    extraction = extract_run_answer(
        make_production_answer_run("本学期没有你的考试安排。", with_tool_call=True)
    )

    assert extraction.answer == "本学期没有你的考试安排。"


@pytest.mark.parametrize(
    "outputs",
    [
        {"messages": [{"type": "human", "content": "在吗"}]},
        {"messages": [{"type": "ai", "content": "   "}]},
        {"answer": ""},
        {"messages": [{"type": "ai", "content": ""}], "answer": "  "},
        {},
    ],
)
def test_extract_answer_reports_missing_with_the_agreed_reason(outputs: Any) -> None:
    """「没有回答」是缺数据，不是 0 分；原因串与用户侧约定逐字一致。"""

    extraction = extract_run_answer(SimpleNamespace(outputs=outputs, error=None))

    assert extraction.answer == ""
    assert extraction.unreadable == MISSING_ANSWER_REASON == "缺少最终回答"


def test_extract_answer_keeps_the_diagnostics_of_a_failed_run() -> None:
    extraction = extract_run_answer(SimpleNamespace(outputs=None, error="TimeoutError: upstream"))

    assert extraction.answer == ""
    assert extraction.unreadable is not None
    assert "TimeoutError" in extraction.unreadable


def test_extract_answer_reports_structurally_invalid_outputs() -> None:
    """字段在、但结构读不出回答：给出「结构非法」而不是谎报「缺少最终回答」。"""

    extraction = extract_run_answer(
        SimpleNamespace(outputs={"messages": "不是数组", "answer": 42}, error=None)
    )

    assert extraction.unreadable is not None
    assert "结构非法" in extraction.unreadable


# --------------------------------------------------- 9.2 判定结果的规整


@pytest.mark.parametrize("score", [0.0, 0.25, 0.6, 0.85, 1.0])
def test_to_judgement_accepts_scores_inside_the_unit_interval(score: float) -> None:
    judgement = to_judgement({"score": score, "reason": "理由"})

    assert judgement.score == pytest.approx(score)


def test_to_judgement_carries_the_reason_through() -> None:
    """结构化输出对象与 dict 两种形态都要能读，reason 必须原样带出。"""

    judgement = to_judgement(AnswerRelevanceVerdict(score=0.8, reason="围绕问题作答"))

    assert judgement.score == pytest.approx(0.8)
    assert judgement.reason == "围绕问题作答"


@pytest.mark.parametrize(
    "score",
    [1.7, -0.1, float("nan"), float("inf"), True, False, "0.5", None],
)
def test_to_judgement_refuses_illegal_scores_instead_of_guessing(score: Any) -> None:
    """越界 / 非数字 / 布尔都记成「无法判定」，而不是猜一个分数或当成 0 分。"""

    judgement = to_judgement({"score": score, "reason": "模型理由"})

    assert judgement.score is None
    assert judgement.reason
    assert "模型理由" in judgement.reason


# --------------------------------------------------- 9.3 Judge 本身


def test_judge_parses_the_structured_verdict_through_function_calling() -> None:
    """真实 LangChain 结构化输出链路：模型调用评测函数 -> 解析出 score / reason。"""

    judge = AnswerRelevanceJudge(
        ScriptedChatModel.scripted(verdict_tool_call(0.9, "直接回答了开放时间"))
    )

    judgement = judge.judge("图书馆开放时间是什么？", "图书馆 8:00-22:00。")

    assert judgement.score == pytest.approx(0.9)
    assert "开放时间" in judgement.reason


def test_judge_asks_with_the_question_and_the_answer_only() -> None:
    """判定输入严格是「问题 + 回答」：提示词里应同时出现两者。"""

    model = ScriptedChatModel.scripted(verdict_tool_call(0.7, "基本切题"))
    AnswerRelevanceJudge(model).judge("图书馆开放时间是什么？", "周一到周五 8:00-22:00。")

    prompt = "\n".join(str(message.content) for message in model.received[0])

    assert "图书馆开放时间是什么？" in prompt
    assert "周一到周五 8:00-22:00。" in prompt


def test_judge_binds_the_structured_output_only_once() -> None:
    """结构化输出只绑定一次：多条用例不会各绑一次，也不会重复调用绑定。"""

    llm = StubLLM(result=AnswerRelevanceVerdict(score=0.8, reason="ok"))
    judge = AnswerRelevanceJudge(llm)

    judge.judge("q1", "a1")
    judge.judge("q2", "a2")

    assert llm.bind_count == 1
    assert llm.invoke_count == 2


def test_judge_uses_function_calling_not_json_schema() -> None:
    """DeepSeek 不接受 json_schema 响应格式，因此必须显式走 function_calling。"""

    llm = StubLLM(result=AnswerRelevanceVerdict(score=0.5, reason="ok"))
    AnswerRelevanceJudge(llm).judge("q", "a")

    assert llm.methods == ["function_calling"]


def test_judge_without_a_question_or_answer_never_calls_the_model() -> None:
    llm = StubLLM(result=AnswerRelevanceVerdict(score=0.9, reason="ok"))
    judge = AnswerRelevanceJudge(llm)

    missing_question = judge.judge("   ", "有回答")
    missing_answer = judge.judge("有问题", "  ")

    assert missing_question.score is None and "问题" in missing_question.reason
    assert missing_answer.score is None and "回答" in missing_answer.reason
    assert llm.bind_count == 0
    assert llm.invoke_count == 0


def test_judge_degrades_permanently_when_the_model_cannot_bind() -> None:
    """供应商不支持 structured output：绑定失败即永久不可用，且不反复重试。"""

    llm = StubLLM(bind_error=NotImplementedError("provider 不支持结构化输出"))
    judge = AnswerRelevanceJudge(llm)

    first = judge.judge("q", "a")
    second = judge.judge("q", "a")

    assert first.score is None and second.score is None
    assert "function_calling" in first.reason
    assert llm.bind_count == 1


def test_judge_runtime_failure_is_not_a_permanent_degradation() -> None:
    """单次调用失败只影响该条：下一次仍会真的调用模型（网络抖动不该永久降级）。"""

    llm = StubLLM(
        result=AnswerRelevanceVerdict(score=0.85, reason="切题"),
        invoke_error=RuntimeError("connection reset"),
    )
    judge = AnswerRelevanceJudge(llm)

    failed = judge.judge("q", "a")
    assert failed.score is None
    assert "调用失败" in failed.reason

    llm.invoke_error = None
    recovered = judge.judge("q", "a")

    assert recovered.score == pytest.approx(0.85)
    assert llm.invoke_count == 2


def test_judge_never_records_an_out_of_range_score_as_zero() -> None:
    llm = StubLLM(result=AnswerRelevanceVerdict(score=1.7, reason="越界"))
    judgement = AnswerRelevanceJudge(llm).judge("q", "a")

    assert judgement.score is None
    assert "越界" in judgement.reason


# --------------------------------------------------- 9.4 评估器（rag_answer_relevance）


@pytest.mark.parametrize(
    ("score", "band"),
    [(1.0, "high"), (0.8, "high"), (0.5, "mid"), (0.3, "low"), (0.1, "low"), (0.0, "low")],
)
def test_answer_relevance_reflects_the_judged_relevance(score: float, band: str) -> None:
    """高相关 -> 高分；部分相关 -> 中分；答非所问 -> 低分（含 0）。"""

    judge = FakeJudge(AnswerRelevanceJudgement(score=score, reason=f"{band} 判定"))
    output = build_answer_relevance_evaluator(judge)(
        make_production_answer_run("回答正文"), make_example("问题？")
    )

    assert output["key"] == ANSWER_RELEVANCE_KEY
    assert output["score"] == pytest.approx(score)
    assert output["comment"] == f"{band} 判定"


def test_answer_relevance_output_contract_is_key_score_comment() -> None:
    output = build_answer_relevance_evaluator(FakeJudge())(
        make_explicit_answer_run("回答"), make_example("问题？")
    )

    assert set(output) == {"key", "score", "comment"}
    assert isinstance(output["comment"], str) and output["comment"]


def test_answer_relevance_judges_the_question_and_the_final_answer() -> None:
    judge = FakeJudge(AnswerRelevanceJudgement(score=0.9, reason="ok"))

    build_answer_relevance_evaluator(judge)(
        make_explicit_answer_run("图书馆 8:00-22:00。"), make_example("图书馆开放时间？")
    )

    assert judge.calls == [("图书馆开放时间？", "图书馆 8:00-22:00。")]


def test_answer_relevance_never_uses_the_retrieved_documents() -> None:
    """判定输入里**没有**检索片段：那是 Faithfulness / Citation 的输入，混进来口径就变了。"""

    judge = FakeJudge(AnswerRelevanceJudgement(score=0.9, reason="ok"))
    run = SimpleNamespace(
        outputs={
            "answer": "体育馆 6:00-21:00。",
            "sources": [
                {
                    "source": "campus_sports.pdf",
                    "title": "体育场馆",
                    "score": 0.9,
                    "snippet": "开放时间……",
                }
            ],
        },
        error=None,
    )

    build_answer_relevance_evaluator(judge)(run, make_example("体育馆什么时候开放？"))

    question, answer = judge.calls[0]
    assert (question, answer) == ("体育馆什么时候开放？", "体育馆 6:00-21:00。")
    assert "campus_sports.pdf" not in answer
    assert "开放时间……" not in answer


def test_answer_relevance_returns_none_when_the_answer_is_missing() -> None:
    """Run 缺回答：``score=None`` + 「无法判定：缺少最终回答」——**不是 0 分**。"""

    run = SimpleNamespace(
        outputs={"messages": [{"type": "human", "content": "问题？"}]}, error=None
    )

    output = build_answer_relevance_evaluator(FakeJudge())(run, make_example("问题？"))

    assert output["score"] is None
    assert output["comment"] == f"无法判定：{MISSING_ANSWER_REASON}"


def test_answer_relevance_returns_none_when_the_judge_cannot_decide() -> None:
    judge = FakeJudge(AnswerRelevanceJudgement(score=None, reason="评测模型调用失败"))

    output = build_answer_relevance_evaluator(judge)(
        make_explicit_answer_run("回答"), make_example("问题？")
    )

    assert output["score"] is None
    assert output["comment"] == "无法判定：评测模型调用失败"


def test_answer_relevance_returns_none_when_the_judge_raises() -> None:
    """Judge 抛异常也不能让整轮评估失败，更不能伪装成低分。"""

    judge = FakeJudge(error=RuntimeError("boom"))

    output = build_answer_relevance_evaluator(judge)(
        make_explicit_answer_run("回答"), make_example("问题？")
    )

    assert output["score"] is None
    assert output["comment"].startswith("无法判定")
    assert "RuntimeError" in output["comment"]


def test_answer_relevance_returns_none_when_the_example_has_no_question() -> None:
    judge = FakeJudge()

    output = build_answer_relevance_evaluator(judge)(
        make_explicit_answer_run("回答"), SimpleNamespace(inputs={}, outputs={})
    )

    assert output["score"] is None
    assert "question" in output["comment"]
    assert judge.calls == []


@pytest.mark.parametrize("score", [0.0, 0.3, 0.7, 1.0])
def test_answer_relevance_scores_stay_inside_the_unit_interval(score: float) -> None:
    judge = FakeJudge(AnswerRelevanceJudgement(score=score, reason="ok"))
    output = build_answer_relevance_evaluator(judge)(
        make_explicit_answer_run("回答"), make_example("问题？")
    )

    assert SCORE_MIN <= output["score"] <= SCORE_MAX
    assert output["score"] is not None


# --------------------------------------------------- 9.5 回答 target


def test_answer_target_shapes_the_generated_answer() -> None:
    """target 输出 = 回答 + 引用 + 条数，且能被 ``extract_run_answer`` 直接读回。"""

    service = FakeRAGService(
        answer="图书馆 8:00-22:00。",
        sources=({"source": "campus_rules.md", "title": "规章", "score": 0.9},),
    )
    target = build_answer_target(answer_fn=service.answer)

    outputs = target({"question": "图书馆开放时间？"})

    assert set(outputs) == {"answer", "sources", "retrieved"}
    assert outputs["answer"] == "图书馆 8:00-22:00。"
    assert outputs["retrieved"] == 1
    assert outputs["sources"][0]["source"] == "campus_rules.md"
    assert extract_run_answer(SimpleNamespace(outputs=outputs, error=None)).answer == "图书馆 8:00-22:00。"
    assert service.calls and service.calls[0][0] == "图书馆开放时间？"


def test_answer_target_skips_generation_when_the_question_is_missing() -> None:
    called: list[str] = []
    target = build_answer_target(answer_fn=called.append)

    with pytest.raises(ValueError, match="question"):
        target({"question": "   "})

    assert called == []


def test_answer_target_coerces_a_non_text_answer_to_empty() -> None:
    """非字符串回答归一为空串：让它走「缺少最终回答」这条明确路径，而不是崩在类型上。"""

    target = build_answer_target(
        answer_fn=lambda question: SimpleNamespace(answer=None, sources=(), retrieved=0)
    )

    outputs = target({"question": "问题？"})

    assert outputs["answer"] == ""
    assert outputs["retrieved"] == 0


# --------------------------------------------------- 9.6 最小验证的选样


def test_answer_selection_picks_the_two_named_cases_by_case_id() -> None:
    """按 case_id 精确挑选，而不是「取前两条」——数据集增删不会让选样漂移。"""

    examples = [
        make_example("图书馆开放时间？", "campus_rules.md", case_id="library-hours"),
        make_example("体育馆开放时间？", "campus_sports.pdf", case_id="gym-hours"),
        make_example("奖学金怎么评？", "scholarship.md", case_id="scholarship"),
    ]

    picked = select_examples(examples, selection=ANSWER_SELECTION)

    assert [item.metadata["case_id"] for item in picked] == list(ANSWER_VERIFICATION_CASE_IDS)
    assert [item.inputs["question"] for item in picked] == [
        "图书馆开放时间？",
        "体育馆开放时间？",
    ]


def test_answer_selection_rejects_a_dataset_without_the_named_cases() -> None:
    examples = [make_example("奖学金怎么评？", "scholarship.md", case_id="scholarship")]

    with pytest.raises(ValueError, match="library-hours"):
        select_examples(examples, selection=ANSWER_SELECTION)


# --------------------------------------------------- 9.7 整条编排（离线）


def _answer_client() -> FakeEvaluationClient:
    """两条主题不同的真实用例形状（覆盖「知识规章」与「体育场馆」两类问题）。"""

    return seeded_client(
        library=make_example("图书馆开放时间？", "campus_rules.md", case_id="library-hours"),
        gym=make_example("体育馆开放时间？", "campus_sports.pdf", case_id="gym-hours"),
    )


def test_answer_experiment_submits_only_the_answer_evaluator() -> None:
    client = _answer_client()
    service = FakeRAGService(answer="图书馆 8:00-22:00。")
    judge = FakeJudge(AnswerRelevanceJudgement(score=0.9, reason="直接回答了开放时间"))
    evaluator = build_answer_relevance_evaluator(judge)

    summary = run_answer_relevance_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_answer_target(answer_fn=service.answer),
        evaluator=evaluator,
    )

    call = client.evaluate_calls[0]
    assert len(call["data"]) == 2
    assert call["evaluators"] == [evaluator]
    assert [fn.__name__ for fn in call["evaluators"]] == ["rag_answer_relevance"]
    assert [fn.__name__ for fn in call["evaluators"]] != [
        fn.__name__ for fn in DEFAULT_EVALUATORS
    ]
    assert call["metadata"]["selection"] == ANSWER_SELECTION
    assert call["metadata"]["evaluator_keys"] == list(ANSWER_EVALUATOR_KEYS)
    assert "k" not in call["metadata"]  # 检索窗口不属于回答实验

    assert summary.title == ANSWER_EXPERIMENT_TITLE
    assert summary.experiment_name == f"{ANSWER_EXPERIMENT_PREFIX}-offline"
    assert summary.cases == 2
    assert summary.scores[ANSWER_RELEVANCE_KEY] == (pytest.approx(0.9), pytest.approx(0.9))


def test_answer_report_lists_only_the_answer_metric() -> None:
    """报告只列回答相关性一个指标：不与检索指标混在一张表里，也不合成总分。"""

    client = _answer_client()
    service = FakeRAGService(answer="体育馆 6:00-21:00。")

    summary = run_answer_relevance_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_answer_target(answer_fn=service.answer),
        evaluator=build_answer_relevance_evaluator(
            FakeJudge(AnswerRelevanceJudgement(score=0.75, reason="切题"))
        ),
    )
    report = summary.format_report()

    assert report.startswith(f"{ANSWER_EXPERIMENT_TITLE}\n")
    assert f"{ANSWER_RELEVANCE_KEY}: 0.750  (n=2)" in report
    for key in DEFAULT_EVALUATOR_KEYS:
        assert key not in report


def test_answer_experiment_skips_undefined_judgements_from_the_average() -> None:
    """判不出来的条目（``score=None``）不进分数集合，均值不被伪造出来。"""

    client = _answer_client()
    judge = FakeJudge(error=RuntimeError("network"))

    summary = run_answer_relevance_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_answer_target(answer_fn=FakeRAGService(answer="回答").answer),
        evaluator=build_answer_relevance_evaluator(judge),
    )

    assert summary.cases == 2
    assert summary.scores.get(ANSWER_RELEVANCE_KEY, ()) == ()
    assert f"{ANSWER_RELEVANCE_KEY}: (无有效分数)" in summary.format_report()


def test_retrieval_experiment_still_submits_the_five_metrics_unchanged() -> None:
    """新增回答相关性后，检索实验提交的指标一字不变（两次实验互不混合）。"""

    client = seeded_client(positive=make_example("图书馆开放时间？", "campus_rules.md"))
    retriever = retriever_with(
        {"图书馆开放时间？": [(make_document("正文", source="campus_rules.md"), 0.10)]}
    )

    summary = run_retrieval_evaluation(
        client=client,
        dataset_name=evaluators_module_dataset_name(),
        target=build_retrieval_target(retriever=retriever),
    )

    assert [fn.__name__ for fn in client.evaluate_calls[0]["evaluators"]] == list(
        DEFAULT_EVALUATOR_KEYS
    )
    assert summary.title == "LangSmith RAG Evaluation"
    assert ANSWER_RELEVANCE_KEY not in summary.format_report()


# --------------------------------------------------- 9.8 CLI 接线


def test_answer_evaluation_flag_defaults_to_off() -> None:
    assert cli._parse_args([]).evaluate_rag_answer is False
    assert cli._parse_args(["你好"]).evaluate_rag_answer is False
    assert cli._parse_args(["--evaluate-rag-answer"]).evaluate_rag_answer is True


def test_answer_flag_is_dispatched_without_building_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["--evaluate-rag-answer"]) == 0
    assert observed["answer"] == [True]
    assert observed["langsmith"] == []
    assert observed["runtime"] == []


def test_answer_flag_rejects_langsmith_full(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """回答相关性没有全量模式：同时传 --langsmith-full 属于参数错误，不是静默降级。"""

    _patch_main_for_dispatch(monkeypatch)

    assert cli.main(["--evaluate-rag-answer", "--langsmith-full"]) == 2
    assert "--evaluate-rag-answer" in capsys.readouterr().err


def test_normal_run_never_touches_the_answer_evaluation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """普通一轮对话照旧：不建评估客户端、不装配评估用 RAG 服务、不跑回答评估。"""

    monkeypatch.setattr(
        cli, "build_dataset_client", lambda: pytest.fail("普通对话不该碰 LangSmith 评估")
    )
    monkeypatch.setattr(
        cli,
        "run_answer_relevance_evaluation",
        lambda **kwargs: pytest.fail("普通对话不该跑回答相关性评估"),
    )
    monkeypatch.setattr(
        cli, "build_rag_service", lambda **kwargs: pytest.fail("普通对话不该装配评估用 RAG 服务")
    )

    class FakeRuntime:
        def invoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
            return {"messages": [AIMessage(content="你好。")], "intent": "chat"}

    assert cli._run_once(FakeRuntime(), "你好", "session-1") == 0
    assert "你好。" in capsys.readouterr().out


def test_judge_llm_reuses_the_existing_config_and_only_zeroes_the_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judge 复用既有 LLM 配置，只把温度固定为 0：不存在第二套模型配置。"""

    captured: list[dict[str, Any]] = []

    class FakeModel:
        def model_copy(self, *, update: dict[str, Any]) -> str:
            captured.append(dict(update))
            return "judge-llm"

    monkeypatch.setattr(cli, "build_llm", lambda: FakeModel())

    assert cli._judge_llm() == "judge-llm"
    assert captured == [{"temperature": JUDGE_TEMPERATURE}]
    assert JUDGE_TEMPERATURE == 0.0


def test_answer_entry_reports_a_configuration_error(capsys: pytest.CaptureFixture[str]) -> None:
    """未配置 LangSmith 时给清晰提示并以配置错误码退出（不是崩溃）。"""

    exit_code = cli._run_evaluate_rag_answer()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "配置错误" in captured.err
    assert "LANGSMITH_API_KEY" in captured.err
    assert FAKE_API_KEY not in captured.err


def test_answer_entry_prints_the_report_without_leaking_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """整条 CLI 链路（含真实 Judge 结构化输出）跑通，且密钥不出现在输出里。"""

    client = _answer_client()
    service = FakeRAGService(
        answer="图书馆 8:00-22:00。",
        sources=({"source": "campus_rules.md", "title": "规章", "score": 0.9},),
    )
    monkeypatch.setenv("LANGSMITH_API_KEY", FAKE_API_KEY)
    get_langsmith_settings.cache_clear()
    monkeypatch.setattr(cli, "build_dataset_client", lambda: client)
    monkeypatch.setattr(cli, "build_rag_service", lambda **kwargs: service)
    monkeypatch.setattr(
        cli,
        "_judge_llm",
        lambda: ScriptedChatModel.scripted(verdict_tool_call(0.9, "直接回答了开放时间")),
    )

    assert cli._run_evaluate_rag_answer() == 0

    report = capsys.readouterr().out
    assert ANSWER_RELEVANCE_KEY in report
    assert "Experiment: " in report
    assert FAKE_API_KEY not in report


# =============================================== 10. 回答相关性的架构守卫


def test_answer_relevance_is_kept_out_of_the_retrieval_evaluators() -> None:
    """回答相关性是独立指标：既不进检索默认集合，也不参与「五个指标」的口径。"""

    assert ANSWER_EVALUATOR_KEYS == (ANSWER_RELEVANCE_KEY,)
    assert ANSWER_RELEVANCE_KEY not in DEFAULT_EVALUATOR_KEYS
    assert ANSWER_RELEVANCE_KEY not in {fn.__name__ for fn in DEFAULT_EVALUATORS}
    assert DEFAULT_EVALUATOR_KEYS == (
        HIT_AT_1_KEY,
        HIT_AT_K_KEY,
        PRECISION_KEY,
        RECALL_KEY,
        NEGATIVE_SAMPLE_KEY,
    )


def test_answer_judge_module_keeps_the_evaluation_layer_boundary() -> None:
    """Judge 模块不得越过 Retriever 直接依赖向量库 / Embedding / Agent / 模型工厂。"""

    path = Path(evaluators_module.__file__).with_name("answer_judge.py")
    assert path.is_file()

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
    import_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for token in forbidden:
            assert token.lower() not in line.lower(), (
                f"answer_judge.py 越层依赖了 {token}: {line.strip()}"
            )


def test_answer_judge_module_contains_no_tracing_api() -> None:
    source = Path(evaluators_module.__file__).with_name("answer_judge.py").read_text(
        encoding="utf-8"
    )

    leaked = [token for token in _TRACING_ONLY_TOKENS if token in source]
    assert leaked == [], f"Judge 模块出现了 tracing 专有 API: {leaked}"


def test_answer_judge_module_path_is_inside_the_evaluation_package() -> None:
    """Judge 与适配层同在评估包内：架构守卫按包扫描，路径必须在其中。"""

    expected = Path(settings_module.app_root()).joinpath(
        "app", "rag", "evaluation", "answer_judge.py"
    )

    assert Path(evaluators_module.__file__).with_name("answer_judge.py") == expected


def test_answer_evaluation_never_touches_tracing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跑一遍回答相关性评估：一个追踪相关的环境变量都不该被写。"""

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    before = {key: os.environ.get(key) for key in _TRACING_ENV_KEYS}

    build_answer_relevance_evaluator(FakeJudge())(
        make_explicit_answer_run("回答"), make_example("问题？")
    )

    after = {key: os.environ.get(key) for key in _TRACING_ENV_KEYS}
    assert after == before


def test_answer_evaluator_reads_the_answer_from_the_production_graph_state() -> None:
    """与真实图对照：Judge 读到的就是 knowledge 分支的回答，且该回答一字不变。"""

    from app.agent.graph import build_graph

    rag_service = FakeRAGService(
        answer="图书馆开放时间为周一至周五 8:00–22:00。",
        sources=({"source": "campus_rules.md", "title": "校园规章", "score": 0.9},),
    )
    graph = build_graph(
        llm=ScriptedChatModel.scripted(),
        intent_classifier=StaticIntentClassifier("knowledge"),
        rag_service=rag_service,
        tools=(),
    )
    state = graph.invoke(
        {"messages": [HumanMessage(content="图书馆开放时间？")]},
        config={"configurable": {"thread_id": "answer-eval-bypass"}},
    )

    judge = FakeJudge(AnswerRelevanceJudgement(score=0.95, reason="直接回答"))
    output = build_answer_relevance_evaluator(judge)(
        SimpleNamespace(outputs=dict(state), error=None), make_example("图书馆开放时间？")
    )

    assert output["score"] == pytest.approx(0.95)
    assert judge.calls == [
        ("图书馆开放时间？", "图书馆开放时间为周一至周五 8:00–22:00。")
    ]
    # Agent 的原始回答与引用完全没被评估动作改动
    assert state["messages"][-1].content == "图书馆开放时间为周一至周五 8:00–22:00。"
    assert state["sources"] == [{"source": "campus_rules.md", "title": "校园规章", "score": 0.9}]

