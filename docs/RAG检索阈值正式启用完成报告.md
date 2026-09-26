# RAG 检索阈值正式启用完成报告

**日期**：2026-09-25
**范围**：把 `RAG_SCORE_THRESHOLD` 从「已实现但默认关闭」变成项目**当前正式运行配置**，并补回归测试。
**未做的（按约束）**：LLM-as-a-Judge、Prompt Management、新业务功能、MCP 修改、Spring Boot 修改、Vue；
未修改 `RAGService` / Knowledge Node / LangGraph / MySQL / LangSmith Evaluator 定义；
**未修改 Retriever 核心算法**（核对后未发现实际 bug）。

---

## 1. 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `.env` | 修改（**已 gitignore**） | 追加 `RAG_SCORE_THRESHOLD=0.4`。二进制读写 + 保留 CRLF/无 BOM，并断言「其余 37 行逐字节不变」；**未读取、未输出任何既有密钥** |
| `.env.example` | **新建**（非敏感模板） | 全部配置键的占位版本（`<your-...-key>`），`RAG_SCORE_THRESHOLD=0.4` 标注为「当前正式启用」，并写明语义/判定位置/过滤顺序/标定来源 |
| `.gitignore` | 修改 | `.env` / `.env.*` 仍忽略，新增 `!.env.example` 放行非敏感模板（`git check-ignore` 已确认 `.env` 仍被忽略） |
| `tests/test_config.py` | 修改（**+4 例**） | `0.4` 被原样读入；`.env.example` 必须声明 `0.4`；模板取值必须能通过配置校验；模板中任何 `*KEY*` 只允许留空或写成 `<占位符>` |
| `tests/rag/test_score_threshold.py` | 修改（**+4 例**） | 恰好 `0.4` **保留**、`0.3984375` **被过滤**、`from_settings` 原样透传、默认配置里**不存在写死的业务阈值** |
| `tests/rag/test_evaluation.py` | 修改（**+3 例**） | 阈值开启后负样本误召回归零；同批数据在阈值关闭时确实误召回（对照组）；precision 提升而 hit/recall 不变 |

> 未新增生产代码：阈值实现（配置读取 + 校验 + 检索层过滤）早已存在且正确，本阶段只把它「接上电」。

### 核对结论（为什么没改核心算法）
- `app/config/settings.py:556-560`：`score_threshold = _read_optional_float("RAG_SCORE_THRESHOLD")`，
  并校验 `0.0 <= score_threshold <= 1.0`，越界/非数字抛 `ConfigurationError`（CLI exit 2）。
- `app/rag/retriever/retriever.py::_accepts`：`None` 直接放行；否则 **`score >= threshold` 才保留**（`>=` 而非 `>`）。
- 阈值比较的是**换算后的相关性**，不是原始距离 —— 与既有契约一致，**未发现 bug**，按约束不动核心算法。

---

## 2. 当前 threshold

**`RAG_SCORE_THRESHOLD = 0.4`**（项目当前正式运行配置，写入 `.env` 并作为 `.env.example` 默认值）。

- 语义：分数恒为「越大越相关」且落在 `[0, 1]`；`score >= 0.4` 保留，`score < 0.4` 丢弃。
- **等于阈值保留**（判定为 `>=`）。
- 分数未知（`score is None`）时**保守放行**，不被阈值误杀。
- 顺序：`top_k` **先截断**、阈值**后过滤**，且**不补位**（过线条数可能少于 `top_k`）。
- 位置：判定**只在检索层** `KnowledgeRetriever._accepts`；Knowledge Node / RAGService / Evaluation /
  `main.py` 一律不出现阈值判断。
- 启动日志自证：`RAG | ... score_threshold=0.400`、`检索策略: score_threshold=0.400`。
- **代码里不存在写死的业务阈值**：`0.4` 只出现在配置层（有专门用例固化 `make_rag_settings()` 默认仍为 `None`）。

---

## 3. 本地 Evaluation 前后关键指标

真实 Chroma（`.storage/vectorstore`，集合 `campus_knowledge`）+ 当前 embedding（`text-embedding-v4`），
数据集 15 条（正样本 12 / 负样本 3）。**唯一变量只有阈值**（用进程环境变量空白覆盖来关闭）。

| 指标 | 阈值关闭（`score_threshold=未启用`） | 阈值 `0.4`（当前） | 变化 |
|---|---|---|---|
| Hit@1 | 100.0% | 100.0% | 不变 |
| Hit@3 | 100.0% | 100.0% | 不变 |
| Precision | **75.0%** | **83.3%** | **+8.3pp** |
| Recall | 100.0% | 100.0% | 不变 |
| 负样本误召回 | **3/3** | **0/3** | **−3** |
| 分数标定（达到声明下限） | 3/3 | 3/3 | 不变 |

指标定义**未改动**：`hit@1` = Top1 片段 source 命中；`hit@k` = TopK（按 source 去重）至少命中一个；
`precision` = 去重 source 中命中期望的比例；`recall` = 期望 source 被召回的比例；
负样本（`expected_sources` 为空）四项**未定义且不参与汇总**，只经 `unexpected_hits` 单独统计。

---

## 4. LangSmith 最小 Evaluation 结果

命令：`python main.py --evaluate-rag-langsmith`（默认最小样本，未跑全量）

```
Dataset:    campus-flow-rag-retrieval
Experiment: campus-flow-rag-retrieval-7771967f
用例: 2（正样本 1 / 负样本 1）
rag_hit_at_1:        1.000  (n=1)
rag_hit_at_k:        1.000  (n=1)
rag_precision:       0.500  (n=1)
rag_recall:          1.000  (n=1)
rag_negative_sample: 1.000  (n=1)
```

- 五个 evaluator 全部产出结果；**未使用 LangSmith 侧另造的阈值配置**，阈值直接来自项目当前运行配置
  （`_run_evaluate_rag_langsmith` 在跑之前装配真实检索器，检索器从 `get_rag_settings()` 取阈值）。
- 负样本那条的运行日志为「**检索命中 3 条但均低于阈值 0.4** / 命中=3 召回=0」——
  直接证明过滤确实发生在检索层，而不是在评估层被"解释"掉。
- 全程**未输出任何密钥**（日志只打印 `api_key=已配置`）。

---

## 5. 是否减少 negative unexpected hits

**是，已从 3/3 降到 0/3。**

- 本地：`负样本误召回: 3/3` → `0/3`；负样本明细的 `actual` 由三条来源变为 `(无召回)`。
- LangSmith：`rag_negative_sample` 由 `0.000` 变为 **`1.000`**（窗口内未召回任何片段 = 1.0），
  其余四项对该负样本仍为 `n=0`（保持「不参与汇总」的本地口径）。
- 原因可解释：真实数据里负样本的相似度落在 **0.176–0.35**，**全部低于 0.4**；
  而正样本落在 **0.584–0.768**，**全部高于 0.4** —— 0.4 正处于两者之间的安全分割点。
- 同时**没有代价**：Hit@1 / Hit@3 / Recall 三项不变，仅 Precision 上升。

---

## 6. 全量测试结果

```
682 passed in 65.45s
```

- 671（既有）+ **11（本次新增）**，**零失败、零跳过、零回归**。
- 新增用例要求全部满足：`threshold=0.4` 能读入；边界 `0.4` **能通过**；`0.3984375` **被过滤**；
  负样本行为符合预期（开启后 `unexpected_hits=0`，关闭时为 `1` 作对照）；原有测试全部通过。
- 运行方式（本机约定）：前置 `CODEBUDDY_SAFE_DELETE_ENABLED='0'` + `--basetemp` 指向工作区内目录。

---

## 7. 遗留问题

1. **阈值是绝对分数，会漂移**：换 Embedding 模型 / 维度 / 版本，或改 `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP`，
   都必须重跑 `python main.py --evaluate-rag` **重新标定**（不能沿用 0.4）。
2. **对给不出相关性分数的后端会静默失效**：若后端返回 `score=None`（既无 relevance 也无距离换算函数），
   阈值会因「保守放行」而完全不生效 → 换向量库/后端前必须先确认分数语义。
3. **Precision 仍有 8.3% 的缺口**：15 条里仍有 1/6 的非期望来源在窗口内过线。这是**检索质量问题**
   （多召回），不是阈值问题；要再收敛需动 `top_k` / 切分策略 / 引入混合检索或重排序（本阶段范围外）。
4. **LangSmith 只跑了最小规模**（1 正 1 负）。要全量需显式加 `--langsmith-full`；未跑全量是为了控制额度，
   链路与指标口径已由最小样本和本地全量双重覆盖。
5. **平台侧无「汇总视图」**：五个指标各自独立（刻意不合成总分），跨实验横向对比需在平台上按 key 分别看。
6. `.env.example` 目前尚未提交（`git status` 显示为新增未跟踪文件），需要时自行 `git add`；
   **不要**把真实 `.env` 一起提交（已确认仍被 `.gitignore` 忽略）。
