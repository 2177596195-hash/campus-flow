"""把本地检索评估数据集同步到 LangSmith Dataset。

职责只有一件事——**Dataset 侧**的搬运::

    本地 retrieval_cases.json -> Example 映射 -> Dataset 幂等同步 -> 同步结果

它**不做评估**：不跑 Retriever、不算 hit@1 / precision / recall，也不创建
Experiment（那些属于后续阶段）。本地评估链路与指标口径一字未改
（``evaluator.py`` / ``schema.py`` 均未触碰）。

与第一阶段 tracing 的关系
------------------------
tracing 的接入方式是「进程级写环境变量」，业务代码里不出现任何 SDK 调用；
本模块是**另一件事**——LangSmith 的 Datasets API，一个由 CLI 显式触发的运维动作：
既不上报 trace，也不参与 Agent 运行时链路，更不随普通对话执行。
因此两条架构守卫对「``app/`` 里不得出现 langsmith」给**本文件开了单文件豁免**
（见 ``tests/test_langsmith_tracing.py`` 与 ``tests/rag/test_evaluation.py`` 的注释），
豁免只覆盖本文件，且守卫会额外断言本文件里**不出现任何 tracing API**；
其它模块（节点 / 图 / 工具 / RAG / MCP）的约束完全没有放松。
也正因豁免是单文件的，本包 ``__init__.py`` 刻意**不再导出**本模块
（导出语句本身会把这个名字带进整个包），调用方直接::

    from app.rag.evaluation.langsmith_dataset import sync_rag_dataset

映射口径（沿用本地契约字段名，不新增业务字段）
--------------------------------------------
================  ====================================================
LangSmith 字段     取值
================  ====================================================
``inputs``        ``{"question": <case.question>}``   —— 即用例的查询语句
``outputs``       ``{"expected_sources": [...]}``     —— 期望结果；负样本为空数组
``metadata``      ``case_id`` / ``expected_keywords`` / ``expected_min_score``
                  / ``is_negative``（负样本显式为 ``true``，不靠「空数组」暗示）
================  ====================================================

键名一律复用本地字段名（``question`` / ``expected_sources`` / ``case_id`` …），
不另造 ``query`` 之类的第二套叫法：将来写评估函数时读的就是本地契约里已有的名字。

幂等策略
--------
1. **Dataset 按名字幂等**：先 ``has_dataset`` 查询，已存在就 ``read_dataset`` 复用，
   不存在才 ``create_dataset``——绝不每次运行都建一个同名 Dataset。
2. **Example 按用例稳定标识幂等**：标识 = ``case_id`` 优先，缺省用问题原文
   （与 ``EvaluationCase.label`` 同一规则，不引入第二种标识）。同步前先拉取远端
   现有 Example 建立标识索引，然后逐条判定：标识命中且内容一致 → 跳过；
   内容变了 → ``update_example`` 原地更新；远端没有 → 追加。
   于是重复执行既不会重复创建，也不会无谓改写，条数恒定。
3. **不删除**远端多余条目（本地删掉某条用例，不会连带删除远端 Example）：
   删除不可逆，本阶段只做「新增 / 更新 / 跳过」，多余条数如实报告出来由人决定。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.rag.evaluation.dataset import load_dataset
from app.rag.evaluation.schema import EvaluationCase

#: Dataset 名字（LangSmith 里 Dataset 是工作区级资源，与 tracing 的 project 无关，
#: 因此用固定的项目前缀名，而不是从 LANGSMITH_PROJECT 派生——否则换个 project
#: 就会「多出一个数据集」，同一份用例出现两份，Experiment 结果没法横向比）。
DEFAULT_DATASET_NAME: Final[str] = "campus-flow-rag-retrieval"

#: 创建 Dataset 时的说明文案（写清来源，便于在平台上辨认）
DATASET_DESCRIPTION: Final[str] = (
    "CampusFlow 检索评估数据集（由 data/evaluation/retrieval_cases.json 同步生成）。"
    "inputs.question 为问题，outputs.expected_sources 为期望命中的知识来源，"
    "空数组表示负样本（本就不该命中知识库）。"
)

#: 字段名常量：与本地契约一一对应，避免在多处硬编码字符串
INPUT_QUESTION_KEY: Final[str] = "question"
OUTPUT_SOURCES_KEY: Final[str] = "expected_sources"
METADATA_CASE_ID: Final[str] = "case_id"
METADATA_KEYWORDS: Final[str] = "expected_keywords"
METADATA_MIN_SCORE: Final[str] = "expected_min_score"
METADATA_IS_NEGATIVE: Final[str] = "is_negative"


class DatasetSyncError(RuntimeError):
    """同步前的本地数据问题（如用例标识重复）导致无法安全对齐远端。"""


class DatasetClient(Protocol):
    """本模块用到的 LangSmith 客户端能力（官方 ``langsmith.Client`` 的超集子集）。

    只声明真正调用的六个方法，好处有二：离线测试可以直接注入替身而不碰网络；
    也让「这个模块到底会动远端哪些东西」一眼可见（没有 delete / 没有 run 上报）。
    """

    def has_dataset(self, *, dataset_name: str) -> bool:
        """按名字判断 Dataset 是否已存在。"""

    def create_dataset(self, dataset_name: str, *, description: str | None = ...) -> Any:
        """创建 Dataset，返回带 ``id`` 的对象。"""

    def read_dataset(self, *, dataset_name: str) -> Any:
        """按名字读取 Dataset，返回带 ``id`` 的对象。"""

    def list_examples(self, dataset_id: Any) -> Iterable[Any]:
        """遍历某个 Dataset 下的 Example。"""

    def create_examples(self, *, dataset_id: Any, examples: Sequence[Mapping[str, Any]]) -> Any:
        """批量追加 Example。"""

    def update_example(
        self,
        example_id: Any,
        *,
        inputs: Mapping[str, Any],
        outputs: Mapping[str, Any],
        metadata: Mapping[str, Any],
        dataset_id: Any,
    ) -> Any:
        """原地更新一个 Example。"""


@dataclass(frozen=True, slots=True)
class ExampleSpec:
    """一条用例的远端投影（映射完成、可直接比较与上传）。"""

    #: 用例稳定标识：``case_id`` 优先，缺省用问题原文
    identifier: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    metadata: dict[str, Any]

    @property
    def payload(self) -> dict[str, Any]:
        """``create_examples`` / ``update_example`` 需要的载荷。"""

        return {
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DatasetSyncResult:
    """一次同步的结果（用于 CLI 报告与断言）。"""

    dataset_name: str
    dataset_id: str
    #: True = 本次新建，False = 复用已有
    dataset_created: bool
    #: 本地用例数
    cases: int
    #: 其中负样本数
    negatives: int
    created: int
    updated: int
    unchanged: int
    #: 远端存在、但不在本地数据集里的条数（本模块**不删**，只报告）
    remote_only: int = 0

    @property
    def total_examples(self) -> int:
        """同步后该 Dataset 的 Example 总数。"""

        return self.cases + self.remote_only

    def format_report(self) -> str:
        """命令行展示用的同步报告（首行固定，便于脚本按行解析）。"""

        lines = [
            "LangSmith RAG Dataset",
            "---------------------",
            f"Dataset: {self.dataset_name}",
            f"ID: {self.dataset_id}",
            f"状态: {'新建' if self.dataset_created else '复用已有'}",
            f"本地用例: {self.cases}（负样本 {self.negatives}）",
            f"新增: {self.created} / 更新: {self.updated} / 跳过(已一致): {self.unchanged}",
            f"远端总条数: {self.total_examples}",
        ]
        if self.remote_only:
            lines.append(f"远端额外条目: {self.remote_only}（非本地数据集用例，本工具不删除）")
        return "\n".join(lines)


def case_identifier(case: EvaluationCase) -> str:
    """用例的稳定标识：``case_id`` 优先，缺省用问题原文。

    与 ``EvaluationCase.label`` 同一规则，因此报告里的用例名与远端 metadata 里的
    ``case_id`` 永远指同一条用例，不需要第二套对照表。
    """

    return case.case_id.strip() or case.question


def build_example_spec(case: EvaluationCase) -> ExampleSpec:
    """把一条本地用例映射为待同步的 Example 投影。"""

    return ExampleSpec(
        identifier=case_identifier(case),
        inputs={INPUT_QUESTION_KEY: case.question},
        outputs={OUTPUT_SOURCES_KEY: list(case.expected_sources)},
        metadata={
            METADATA_CASE_ID: case_identifier(case),
            METADATA_KEYWORDS: list(case.expected_keywords),
            METADATA_MIN_SCORE: case.expected_min_score,
            # 负样本显式标记：只靠 outputs 里的空数组太隐晦，评估函数与人工排查
            # 都需要一个能直接筛选的字段。
            METADATA_IS_NEGATIVE: case.is_negative,
        },
    )


def build_example_specs(cases: Iterable[EvaluationCase]) -> list[ExampleSpec]:
    """批量映射，并拒绝标识重复——标识是幂等的锚点，重复会让对齐变成猜谜。

    数据集本身已禁止重复问题（见 ``parse_cases``），但 ``case_id`` 重复仍可能发生：
    那会让「远端该更新哪一条」失去唯一答案，属于必须当场拒绝的维护事故。
    """

    specs = [build_example_spec(case) for case in cases]
    seen: set[str] = set()
    duplicated: list[str] = []
    for spec in specs:
        if spec.identifier in seen:
            duplicated.append(spec.identifier)
        seen.add(spec.identifier)
    if duplicated:
        raise DatasetSyncError(
            "用例标识重复，无法安全对齐远端: " + ", ".join(sorted(set(duplicated)))
        )
    return specs


def _example_identifier(example: Any) -> str | None:
    """读出远端 Example 的用例标识；读不到（非本工具写入）返回 ``None``。"""

    metadata = getattr(example, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    identifier = metadata.get(METADATA_CASE_ID)
    return identifier if isinstance(identifier, str) and identifier else None


def _remote_metadata(example: Any) -> Mapping[str, Any]:
    metadata = getattr(example, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _example_matches(example: Any, spec: ExampleSpec) -> bool:
    """远端 Example 是否与本地用例逐字段一致。

    比较范围刻意是「本模块写入的那些字段」：inputs / outputs 全等，且 spec 里的每个
    metadata 键都能对上。远端若有额外 metadata 键（平台自己加的）不参与比较，
    否则会把「平台加了个字段」误判成内容变化，每次同步都白写一遍。
    """

    if dict(getattr(example, "inputs", None) or {}) != spec.inputs:
        return False
    if dict(getattr(example, "outputs", None) or {}) != spec.outputs:
        return False
    remote = _remote_metadata(example)
    return all(remote.get(key) == value for key, value in spec.metadata.items())


def sync_rag_dataset(
    cases: Iterable[EvaluationCase] | None = None,
    *,
    client: DatasetClient,
    dataset_name: str = DEFAULT_DATASET_NAME,
) -> DatasetSyncResult:
    """把本地评估数据集幂等同步到 LangSmith Dataset。

    Args:
        cases: 用例列表；省略时读取内置数据集 ``data/evaluation/retrieval_cases.json``。
        client: LangSmith 客户端（或同签名的替身）。**由调用方注入**，
            因此离线测试可以完全不碰网络。
        dataset_name: Dataset 名字；已存在即复用。

    Returns:
        同步结果（Dataset 标识、新增 / 更新 / 跳过条数、总条数）。

    Raises:
        DatasetSyncError: 用例标识重复、数据集文件损坏等本地问题。
    """

    specs = build_example_specs(load_dataset() if cases is None else cases)

    dataset_created = False
    if client.has_dataset(dataset_name=dataset_name):
        dataset = client.read_dataset(dataset_name=dataset_name)
    else:
        dataset = client.create_dataset(dataset_name, description=DATASET_DESCRIPTION)
        dataset_created = True
    dataset_id = getattr(dataset, "id", dataset)

    # 先把远端现有 Example 按用例标识索引好：同步过程是「先读后写」，
    # 与增量索引「先把要写的准备好再动库」同一个道理，避免边写边读。
    remote: dict[str, Any] = {}
    remote_only = 0
    for example in client.list_examples(dataset_id):
        identifier = _example_identifier(example)
        if identifier is None or identifier in remote:
            # 不是本工具写入的（读不到标识），或同一标识出现多条（历史脏数据）：
            # 一律不碰，只计数上报。
            remote_only += 1
            continue
        remote[identifier] = example

    to_create: list[dict[str, Any]] = []
    created = updated = unchanged = 0
    for spec in specs:
        example = remote.pop(spec.identifier, None)
        if example is None:
            to_create.append(spec.payload)
            created += 1
        elif _example_matches(example, spec):
            unchanged += 1
        else:
            client.update_example(
                getattr(example, "id", example),
                inputs=spec.inputs,
                outputs=spec.outputs,
                metadata=spec.metadata,
                dataset_id=dataset_id,
            )
            updated += 1

    if to_create:
        client.create_examples(dataset_id=dataset_id, examples=to_create)

    # 剩下的 remote 就是远端有、本地没有的条目：报告出来，但不删除
    remote_only += len(remote)

    return DatasetSyncResult(
        dataset_name=dataset_name,
        dataset_id=str(dataset_id),
        dataset_created=dataset_created,
        cases=len(specs),
        negatives=sum(1 for spec in specs if spec.metadata[METADATA_IS_NEGATIVE]),
        created=created,
        updated=updated,
        unchanged=unchanged,
        remote_only=remote_only,
    )


def build_dataset_client() -> DatasetClient:
    """按第一阶段已有的 LangSmith 配置创建官方客户端。

    复用（而不是另起一套）：API Key / endpoint 都来自 ``get_langsmith_settings()``，
    与 tracing 用的是同一份配置。区别只有一个——**这里必须真有 Key**：
    追踪是可选观测设施（没配就不上报），而同步数据集没有 Key 就无事可做，
    因此缺 Key 时抛 ``ConfigurationError``，由 CLI 转成清晰的错误提示。

    SDK 与配置入口都在函数内 import：本模块被导入时不读配置、不建连接，
    也不要求 SDK 存在——普通 Agent 运行与单元测试都不会因此产生任何副作用。
    """

    from app.config.settings import ConfigurationError, get_langsmith_settings

    settings = get_langsmith_settings()
    if not settings.api_key:
        raise ConfigurationError(
            "未配置 LangSmith API Key，无法同步评估数据集；"
            "请在 .env 里设置 LANGSMITH_API_KEY（或第一阶段的 LANGCHAIN_API_KEY）后重试。"
        )

    from langsmith import Client

    # 显式传入，不依赖环境变量：同步数据集与「是否开启 trace 上报」无关，
    # 不该因为追踪开关是 false 就以为这里也没配。
    kwargs: dict[str, Any] = {"api_key": settings.api_key}
    if settings.endpoint:
        kwargs["api_url"] = settings.endpoint
    return Client(**kwargs)


__all__ = [
    "DATASET_DESCRIPTION",
    "DEFAULT_DATASET_NAME",
    "DatasetClient",
    "DatasetSyncError",
    "DatasetSyncResult",
    "ExampleSpec",
    "INPUT_QUESTION_KEY",
    "METADATA_CASE_ID",
    "METADATA_IS_NEGATIVE",
    "METADATA_KEYWORDS",
    "METADATA_MIN_SCORE",
    "OUTPUT_SOURCES_KEY",
    "build_dataset_client",
    "build_example_spec",
    "build_example_specs",
    "case_identifier",
    "sync_rag_dataset",
]
