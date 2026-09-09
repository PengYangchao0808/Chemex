# ADR-003：v1 最小化结构 —— 溶解 Application Service、压平单子包、删除别名与遗留回退

| 字段 | 值 |
| --- | --- |
| 状态 | 已定稿 |
| 日期 | 2026-09-09 |
| 取代 | ADR-002 的分层决定（`Skill/CLI/MCP → Application Service → Pipeline → Domain Models` 中的 Application Service 层） |
| 适用版本 | 自 1.0.0a1 起；alpha 窗口内一次性收敛 |

## 1. 结论

v1 冻结前将 `src/chemex_lit` 收敛为 20 个结构单元：

1. **溶解 `application/`**：`ChemExService` 的 run/resume/submit/status 工作流并入 `pipeline.py` 公开函数（`run_pdf()` / `resume_run()` / `submit_files()` / `run_status()`）；cancel 由 CLI 直调 `store.finish()`。CLI 保持薄壳。
2. **压平单子包**：`llm/`（3 文件）→ `llm.py`；`chemistry/`（3 文件）→ `chemistry.py`；`evaluation/`（4 文件）→ `evaluation.py`；`extraction/{text,table,structure}.py` 合并为 `extraction/extractors.py`（仅 `fulfill()` + 构造）。
3. **删除模式别名**：`human-ocsr-agent` / `auto-agent` 不再被 `--mode` 接受，`normalize_mode` 的别名分支删除；v1 契约冻结为 `auto / semi / agent` 三模式。
4. **删除 pre-P0A 指纹回退**：manifest 缺少 `config` 字典的旧 run 目录不再按 `config_sha256` 回退比对，直接报错并提示重建。

## 2. 背景与动机

ADR-002 在 P1 引入 `application/service.py`，意图让 CLI 与未来的 MCP 共享一个应用服务层。两个月的实际演进证明该层没有长出独立语义：

- service 的每个工作流方法都是 pipeline 公开函数的薄转发，加上一层参数重排与状态读取；测试不得不同样逻辑覆盖两遍（`test_service.py` 与 `test_pipeline*.py` 高度重叠）。
- 依赖方向多出一跳，却没有第二个真实消费者（MCP 延后至 P3 且需求未验证）；唯一消费者 CLI 因此被迫绕过或穿透该层。
- `llm/`、`chemistry/`、`evaluation/` 均为"一个公开类/函数 + 若干私有助手"的 3–4 文件包，包开销（`__init__` 再导出、跨文件跳转）超过其边界价值；`extraction/{text,table,structure}.py` 在 P1 死代码删除后只剩 `fulfill()` + 构造，三文件合计不足百行。

alpha 期是破坏性收敛的最后窗口；v1 之后模块布局将难以再动。

## 3. 目标结构（20 个结构单元）

```text
src/chemex_lit/
├── __init__.py            # 版本号
├── cli.py                 # Click 薄壳
├── pipeline.py            # 唯一执行内核: run/resume/submit/status/cancel + 阶段编排
├── models.py              # v1 数据契约（不动）
├── store.py               # 原子写与 resume 状态（含 write_raw/write_bytes）
├── config.py / profiles.py / credentials.py / errors.py
├── mineru.py
├── assembly.py / adjudicator.py / review.py
├── extraction/
│   ├── __init__.py        # payload 规范化唯一权威（公开 API）
│   ├── tasks.py
│   └── extractors.py      # Text/Table/Structure 三个 fulfill-only 类
├── llm.py                 # LLMClient + PromptRegistry
├── chemistry.py           # Validator + unique_issues + render
├── evaluation.py          # evaluate_files + load/match
└── resources/             # 配置、提示词、模板
```

## 4. 关键决定

### 4.1 依赖方向简化

ADR-002 的 `Skill/CLI/MCP → Application Service → Pipeline → Domain Models` 简化为：

```text
Skill / CLI（薄壳） → Pipeline（唯一执行内核） → Domain Models
```

- pipeline 是唯一自动/手动编排者的不变量不变；工作流函数（run_pdf/resume_run/submit_files/run_status）成为 pipeline 的公开 API，CLI 逐命令一一对应。
- cancel 无编排逻辑（仅 `store.finish("cancelled")`），CLI 直接调用，不为其制造 pipeline 函数。
- 未来 MCP（ADR-002 P3）同样直接消费 pipeline 公开函数，不再需要中间层。

### 4.2 压平与合并的边界

- 压平只改变模块物理布局，不改变任何公开行为：`chemex_lit.llm`、`chemex_lit.evaluation`、`chemex_lit.chemistry` 的顶层导入路径保持可用。
- `chemex_lit.chemistry.validate` / `chemex_lit.chemistry.render` 子模块路径消失，统一为 `chemex_lit.chemistry`。
- `chemex_lit.extraction.{text,table,structure}` 子模块路径消失，统一为 `chemex_lit.extraction.extractors`。
- `chemex_lit.application` 包删除。README 已声明"Python modules other than `chemex_lit.models` are implementation details"，以上路径不属于稳定公共契约。

### 4.3 别名删除的语义

- `--mode human-ocsr-agent` / `--mode auto-agent` 从"接受并归一化"变为 Click 直接拒绝（`--help` 只列三模式）。
- 旧 manifest 中残留别名 mode 的 run 目录：resume 时报错并提示以规范模式重建；不再静默迁移（原 store 的别名迁移分支同步删除）。

### 4.4 遗留回退删除的语义

- `ArtifactStore.initialise` 要求 manifest 含 `config` 字典并逐键比对；pre-P0A 产物（仅有 `config_sha256`）一律报错："旧格式 run 目录不可 resume，请以相同输入新建 run"。
- alpha 期接受该断链；CHANGELOG 明示。

## 5. 不变量核对（沿用 ADR-002 §6，无修订）

单一 `Pipeline`；公开记录经 `chemex_lit.models` 校验；验证器报告问题、不做生成修复；装配确定性；裁决 acceptance-only；运行时资源走 `importlib.resources`；凭证仅来自环境与用户凭证库；一切写入过 `ArtifactStore`；`completed_empty ≠ success`；人工结构以任务边界 Submission 进入；提取器为任务履行者。

ADR-002 §8 发布门槛逐条复核：条款 2（三模式从 validation 起同码、任务抽象统一）与条款 5（CLI 独立可用、reasoning 缺省回退）在别名删除后仍成立——别名从来不是第三模式之外的独立路径，只是输入归一化。

## 6. 后果

- 测试与脚本更新：`scripts/smoke-cli.py`、`tests/test_service.py`（改道 pipeline 公开函数并更名）、`tests/test_extraction.py` / `test_llm.py` / `test_mineru.py` 等导入路径同步。
- pipeline.py 增至约 1500 行：内部按节分区（workflow API / 阶段编排 / 任务状态），可接受——单元数优先于行数。
- 旧 run 目录（别名 mode 或 pre-P0A manifest）不可 resume；报错信息给出重建命令。
- 文档对齐：AGENTS.md 生产结构树、`docs/architecture.md`、README 别名段落、CHANGELOG 迁移说明。

## 7. 验证

- `pytest` 全绿且覆盖率 ≥ 85%；`ruff check src tests`；`pyright src/chemex_lit`；`python -m build` + `twine check`。
- `scripts/smoke-cli.py` 无网络端到端跑通 run → submit → resume → review。
- grep 证明：无 `.extract(` 调用；无 `_utc_now` 多份定义；src 内除 store.py 外无 write_text/MolToFile 直写；pipeline 不 import 下划线符号。

## 8. 参考

- ADR-002（分层架构与被取代的 Application Service 决定）
- `.omo/plans/exclean-v1-minimal-refactor.md`（执行方案与逐阶段验证门）
