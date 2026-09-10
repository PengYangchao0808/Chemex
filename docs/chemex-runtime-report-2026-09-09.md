# ChemEx-Lit 最新版本运行时报告

> 本报告基于 2026-09-09 对 ChemEx-Lit 当前 HEAD 的直接验证产物编写。
> 标记 `[E]` 表示本轮直接证据（来自 `/tmp/opencode/chemex-validation/20260909T20260909T102740Z/` 及当前源码），
> 标记 `[S]` 表示用户此前提供、本轮未重新执行的记录。

## 1. 报告信息

| 项目 | 内容 |
| --- | --- |
| 项目 | ChemEx-Lit |
| 当前包版本（pyproject.toml） | `0.0.0` [E] |
| CLI / manifest 运行版本 | `1.0.0a1`（来自 `__init__.py`）[E] |
| 报告日期 | 2026-09-09 |
| 报告文件 | `docs/chemex-runtime-report-2026-09-09.md` / `.pdf` [E] |
| 运行平台 | Linux（WSL），Python 3.12.12 [E] |
| 当前 Git HEAD | `efd3fd1`（tag `v0.0.0-2-gefd3fd1`）[E] |
| 验证日志 Git 基线 | `061a648`（tag `v0.0.0-1-g061a648`）[E] |
| 核心目标 | 从化学文献 PDF 中提取可追溯、可验证的反应记录 |
| 验证范围 | 三模式 CLI 端到端、单元/契约测试套件、静态分析 |
| 本轮验证环境 | 无真实 MinerU / LLM / 网络调用 [E] |

## 2. 证据分级

| 级别 | 含义 | 本轮使用 |
| --- | --- | --- |
| `[E]` | 本轮直接运行、原始输出落盘 | 模式测试、CLI 驱动、完整套件、Ruff、Pyright、manifest、提交产物 |
| `[S]` | 用户此前提供、本轮未重新执行 | RDKit 2026.3.3 版本、MinerU 401、LLM key 缺失、ExternalServiceError 行为、Windows/Unicode 测试 |

本报告不执行任何网络请求，不声称云端服务可用性。

## 3. 版本与环境

### 3.1 基线信息 `[E]`

```text
Python  3.12.12  (/root/miniforge3/bin/python3)
Package 1.0.0a1  (src/chemex_lit/__init__.py)
pyproject.toml   0.0.0  (发布基线)
Git      current efd3fd1  v0.0.0-2-gefd3fd1
         evidence baseline 061a648  v0.0.0-1-g061a648
pytest   8.3.4
```

### 3.2 依赖版本

当前报告整理时重新读取了 RDKit 版本；用户此前总结中的版本另列为历史记录：

| 依赖 | 版本 | 来源 |
| --- | --- | --- |
| RDKit（当前解释器） | 2025.09.3 | `[E]` `packaging-latest.txt` |
| RDKit（用户此前总结） | 2026.3.3 | `[S]` pip freeze，未在本轮复核 |
| Pyright | 最新（`/root/miniforge3/lib/...`） | `[E]` baseline |
| Ruff | `~=0.15.14`（pyproject.toml 约束） | `[E]` |

## 4. 三模式架构

### 4.1 模式与通道所有权

| 模式 | text 通道 | table 通道 | structure 通道 | adjudication 通道 |
| --- | --- | --- | --- | --- |
| `auto` | CLI 模型 | CLI 模型 | CLI 模型 | CLI 模型 |
| `semi` | CLI 模型 | CLI 模型 | 宿主 Agent | CLI 模型 |
| `agent` | 宿主 Agent | 宿主 Agent | 宿主 Agent | 宿主 Agent |

`auto` 模式下所有 producer_plan.kind 均为 `cli_model`；
`semi` 模式下 structure 为 `host_agent`，其余为 `cli_model`；
`agent` 模式下全部为 `host_agent`。
以上均经 manifest.json producer_plan 直接证实 [E]。

### 4.2 管道流程

```text
PDF
 |
 v
+-------------+
|  Document   |  MinerU 文档解析、证据资产落盘
+-------------+
 |
 v
+-------------+
| Extraction  |  text / table / structure 任务生成与候选提取
+-------------+
 |
 v
+-------------+
| Validation  |  RDKit 解析、规范化、问题报告
+-------------+
 |
 v
+-------------+
|  Assembly   |  确定性候选合并，生成记录
+-------------+
 |
 v
+--------------+
| Adjudication |  可选：accept / keep_review 裁决
+--------------+
 |
 v
+--------------+
| Finalization |  records.jsonl + review.html + provenance
+--------------+
```

状态机：

```text
running
   -> awaiting_input --submit--> ready --resume--> running
   -> success
   -> completed_empty
   -> partial
   -> failed
   -> cancelled
```

### 4.3 通道语义

- `auto`：CI 和批处理场景。所有生成通道由 CLI 内部模型完成，无需外部提交。
- `semi`：text/table 由 CLI 模型处理；structure/OCSR 生成等待宿主提交，适合人工提供 SMILES。
- `agent`：全部生成性任务变成宿主可提交的 task，text/table/structure 各自独立，适合使用 Codex/OpenCode 的视觉和推理能力。

## 5. 端到端验证结果

### 5.1 三模式 CLI 驱动 `[E]`

输入文件：`paper-fixed.pdf`（sha256: `c35b21d6...`）[E]。
测试脚本：`scripts/test_three_modes.py`（工作树中为 `??` 状态，本轮未提交）。

#### 5.1.1 auto 模式

```text
$ chemex-lit run paper-fixed.pdf --mode auto --output-dir .../cli-runs-fixed/auto --json
status: success
records_count: 1
review_count: 0
stages: document=complete, extraction=complete, validation=complete,
        assembly=complete, adjudication=skipped, finalization=complete
elapsed: 0.183s
```

auto 模式直接完成全部阶段，无暂停，无外部提交。验证通过 [E]。

#### 5.1.2 semi 模式

```text
阶段 1: run -> status=awaiting_input
         awaiting: 1 task (channel: structure, task_id: st-889b9fe1)
阶段 2: submit submission-semi.jsonl -> status=ready, applied=2, awaiting=0
阶段 3: resume -> status=success, records=1
elapsed: 0.034s
```

semi 模式正确暂停于 structure 通道，宿主提交 2 条结构候选后恢复成功 [E]。
提交内容仅含结构任务（SMILES），不涉及 reaction 提交 [E]。

#### 5.1.3 agent 模式

```text
阶段 1: run -> status=awaiting_input
         awaiting: 3 tasks
           structure: st-fc8ce24f
           table:     tt-550871be
           text:      tx-31647c86
阶段 2: submit submission-agent.jsonl -> status=ready, applied=4, awaiting=0
阶段 3: resume -> status=success, records=1
elapsed: 0.031s
```

agent 模式正确为 text/table/structure 三个通道各生成一个 task。
宿主提交 3 行 JSONL（text + table + structure），全部 accepted [E]。
裁决（adjudication）在本轮验证中被跳过（`--adjudicate` 未启用）[E]。

补充说明：`09-repo-three-mode-runner.txt` 对应的综合脚本以
`adjudicate=True` 启动并记录 `adjudication_handled=true`，但该输入未产生独立的
`tasks/adjudication.jsonl`，因此本轮没有实际提交 `accept`/`keep_review` 决策。
用户此前总结中的“两阶段裁决已完成”属于 `[S]`，不能由本轮直接证据替代。

#### 5.1.4 综合结果

| 模式 | 初始状态 | 提交后状态 | 最终状态 | records | 耗时 |
| --- | --- | --- | --- | --- | --- |
| auto | success | N/A | success | 1 | 0.183s |
| semi | awaiting_input | ready | success | 1 | 0.034s |
| agent | awaiting_input | ready | success | 1 | 0.031s |

三模式均产出 1 条记录，100% 契约遵守 [E]。

### 5.2 Manifest 一致性 `[E]`

三个 run 的 manifest 共享：

| 字段 | 值 |
| --- | --- |
| run_id | `paper-fixed-c35b21d6` |
| chemex_version | `1.0.0a1` |
| schema_version | `1.0` |
| input_sha256 | `c35b21d6ca39aa7cc3b79a705d989f1a6e88b99ab43988d74048799e3db926a3` |
| config_sha256 | `fde2f93363c44e38a2e222451fc97addae93bb2ddce3306d0c8822cfc51f6add` |
| models | text=deepseek-v4-flash, vision=glm-4v-plus, reasoning=deepseek-v4-pro |
| profile | null（默认配置） |
| models_source | default |

不同之处仅在 mode 和 producer_plan [E]。

### 5.3 初始 CLI 驱动对比 `[E]`

在使用 `paper.pdf`（非 fixed 版本）的首次驱动中，auto 模式返回 `status=partial`、`review_count=1`。
切换到 `paper-fixed.pdf` 后，三模式全部 `status=success`、`review_count=0` [E]。
这说明输入 PDF 的内容完整性直接影响审查队列状态。

### 5.4 模式单元测试 `[E]`

```text
platform linux -- Python 3.12.12, pytest-8.3.4, pluggy-1.6.0
collected 57 items

tests/test_cli_contract.py ..............          [ 24%]
tests/test_routing_modes.py ..........             [ 42%]
tests/test_pipeline_modes.py ........              [ 56%]
tests/test_workflow.py .......                     [ 68%]
tests/test_pipeline.py ...                         [ 73%]
tests/test_cli.py .......                          [ 85%]
tests/test_skill_docs.py ........                  [100%]

57 passed in 2.03s
```

覆盖三模式 producer plan、host route metadata、agent 暂停/提交/resume 语义、manifest/fingerprint 和配置变更保护 [E]。

### 5.5 冒烟测试 `[E]`

```text
[OK]   run --mode semi: status=awaiting_input awaiting=1
[OK]   read tasks/extraction.jsonl: 1 structure task(s)
[OK]   offline validation: submission.jsonl matches the contract
[OK]   submit: status=ready
[OK]   resume: status=success awaiting=0
[OK]   records=1 review=0 provenance=3 manifest.mode=semi
```

run -> submit -> resume -> review 完整链路验证通过 [E]。

### 5.6 凭证检查 `[E]`

| 模式 | RDKit | MinerU key | Text model key | Vision model key |
| --- | --- | --- | --- | --- |
| auto | ok | ok (env) | ok (env) | ok (env) |
| semi | ok | ok (env) | ok (env) | ok (env) |
| agent | ok | ok (env) | N/A | N/A |

agent 模式只需 MinerU key，不检查 text/vision key，因为这些通道交给宿主 Agent [E]。
注意：以上检查确认的是 key 在环境变量中存在，不代表实际调用成功 [E]。

## 6. 测试 / 静态分析 / 打包门禁

### 6.1 完整测试套件 `[E]`

```text
184 passed in 5.54s
Total coverage: 85.14% (required 70%)
```

关键模块覆盖率：

| 模块 | 语句数 | 覆盖率 |
| --- | --- | --- |
| models.py | 176 | 99% |
| profiles.py | 80 | 100% |
| assembly.py | 93 | 95% |
| credentials.py | 125 | 94% |
| extraction/tasks.py | 63 | 97% |
| config.py | 133 | 96% |
| store.py | 178 | 89% |
| review.py | 103 | 90% |
| pipeline.py | 664 | 85% |
| cli.py | 370 | 82% |
| llm.py | 142 | 73% |
| mineru.py | 162 | 52% |

mineru.py 覆盖率最低（52%），因为真实 MinerU 调用被 mock 隔离 [E]。

### 6.2 静态分析 `[E]`

```text
Ruff:   All checks passed!
Pyright: 0 errors, 0 warnings, 0 informations
```

### 6.3 打包验证 `[E]` / `[S]`

本轮在隔离输出目录重新执行了构建与元数据检查，原始记录见
`/tmp/opencode/report-build/packaging-latest.txt`：

- `python -m build --outdir /tmp/opencode/report-build/dist`：sdist 和 wheel 均构建成功 [E]。
- `python -m twine check /tmp/opencode/report-build/dist/*`：wheel 与 sdist 均通过 [E]。
- clean virtual environment 安装 wheel 成功 [S]，本轮未重新执行。
- 安装后 `chemex-lit --version`、`models list` 和 `run --help` 成功 [S]，本轮未重新执行。
- `default_models.yaml` 被纳入构建产物 [E]；clean wheel 安装后的资源读取仍属 [S]。

## 7. 外部服务凭证状态 `[E]` / `[S]`

以下均为此前记录，本轮未重新探测：

| 服务 | 状态 | 说明 |
| --- | --- | --- |
| RDKit | 当前解释器为 2025.09.3 [E]；此前总结为 2026.3.3 [S] | 本地解释器版本记录 |
| MinerU | 实际调用返回 HTTP 401 [S] | 环境变量中 key 存在但服务端拒绝 |
| LLM text/vision | key 未设置 [S] | 实际运行时通过 mock 测试 |
| LLM reasoning | key 未设置 [S] | 同上 |
| ExternalServiceError | 行为正确 [S] | 外部服务不可达时抛出，不会静默失败 |

本轮验证环境中的 `check --mode` 显示 MinerU key 为 ok，但这是因为环境变量已设置。
实际云端调用是否成功取决于服务端凭证有效性 [E] + [S]。

## 8. 跨平台变更记录 `[S]`

以下为此前 Windows/Unicode 测试记录，本轮未在 Windows 上重新执行：

- Windows 下 `chemex-lit --version` 和 `chemex-lit run` 命令正常 [S]。
- 路径中含 Unicode 字符（中文目录名）时 CLI 正常处理 [S]。
- `platformdirs` 自动选择 Windows 配置路径（`%LOCALAPPDATA%\chemex-lit\`）[S]。

CI 矩阵在 ubuntu 和 windows 上运行 Python 3.11-3.13 [S]。

## 9. 风险与局限

### 9.1 当前阻塞

| 优先级 | 风险项 | 说明 |
| --- | --- | --- |
| P0 | Golden paper 真实端到端未完成 | MinerU 云端返回 401，真实 PDF 解析未闭环 [S] |
| P0 | LLM 凭证缺失 | text/vision/reasoning 模型实际调用未验证 [S] |
| P1 | 裁决协议未形成独立任务 | 综合脚本虽传入 `adjudicate=True`，但本次输入未产生 `tasks/adjudication.jsonl`；accept/keep_review 提交链路仍需单独构造歧义样本验证 |
| P2 | 输入 PDF 影响 | `paper.pdf` 导致 partial + review_count=1；`paper-fixed.pdf` 正常 [E] |
| P2 | 测试覆盖率 gap | mineru.py 52%，llm.py 73%，adjudicator.py 33% [E] |

### 9.2 测试边界

测试套件不调用真实 MinerU 或 LLM，这是有意设计：外部服务通过 mock 隔离，确保单元和契约测试可重复 [E]。真实服务验证必须单独执行，且必须保存 run directory、manifest、provenance 和服务配置摘要。

### 9.3 工作树状态

当前工作树存在以下未提交变更，本报告不修改或回退它们：

```text
 M scripts/smoke-cli.py
 M src/chemex_lit/extraction/tasks.py
 M tests/test_cli_contract.py
 M tests/test_credentials.py
?? scripts/test_three_modes.py
```

这些变更与验证过程并行存在，不影响已落盘的证据产物。

## 10. 复现命令

### 10.1 完整测试套件

```bash
python -m pytest                          # 184 passed, 85.14%
python -m ruff check src tests            # All checks passed
python -m pyright src/chemex_lit          # 0 errors, 0 warnings, 0 informations
```

### 10.2 模式聚焦测试

```bash
python -m pytest tests/test_cli_contract.py \
  tests/test_routing_modes.py \
  tests/test_pipeline_modes.py \
  tests/test_workflow.py \
  tests/test_pipeline.py \
  tests/test_cli.py \
  tests/test_skill_docs.py \
  --no-cov                                # 57 passed
```

### 10.3 三模式 CLI 驱动

```bash
# auto
chemex-lit run paper-fixed.pdf --mode auto \
  --output-dir /tmp/.../cli-runs-fixed/auto --json

# semi: run -> submit -> resume
chemex-lit run paper-fixed.pdf --mode semi \
  --output-dir /tmp/.../cli-runs-fixed/semi --json
chemex-lit submit /tmp/.../cli-runs-fixed/semi \
  submission-semi.jsonl --json
chemex-lit resume /tmp/.../cli-runs-fixed/semi --json

# agent: run -> submit -> resume
chemex-lit run paper-fixed.pdf --mode agent \
  --output-dir /tmp/.../cli-runs-fixed/agent --json
chemex-lit submit /tmp/.../cli-runs-fixed/agent \
  submission-agent.jsonl --json
chemex-lit resume /tmp/.../cli-runs-fixed/agent --json
```

### 10.4 凭证检查

```bash
chemex-lit check --mode auto    # ok: RDKit, MinerU, Text, Vision
chemex-lit check --mode semi    # ok: RDKit, MinerU, Text, Vision
chemex-lit check --mode agent   # ok: RDKit, MinerU
```

### 10.5 Markdown / PDF 生成与检查

Markdown 先通过 Python-Markdown 转换为带 A4 打印样式的 HTML，再由本地
Chromium Playwright `page.pdf(format="A4", print_background=True,
prefer_css_page_size=True)` 输出 PDF。PDF 使用 PyMuPDF 检查文本层、中文字符、关键
标题和页数，并重复渲染确认页数稳定。最新输出记录为：

```text
/tmp/opencode/report-build/render-report-final2.txt
/tmp/opencode/report-build/artifacts.sha256
docs/chemex-runtime-report-2026-09-09.pdf
```

本轮 PDF 为 12 页、可搜索文本层，Markdown 与 PDF 的 SHA-256 已写入
`artifacts.sha256` [E]。

## 11. 证据索引

| 文件 | 内容 | 标记 |
| --- | --- | --- |
| `01-baseline.txt` | Python 3.12.12, CLI 1.0.0a1, Git 061a648 | [E] |
| `02-ruff.txt` | Ruff: All checks passed | [E] |
| `02-pyright.txt` | Pyright: 0 errors, 0 warnings, 0 informations | [E] |
| `03-full-suite.txt` | 184 passed, 85.14%, 6.01s | [E] |
| `04-mode-tests.txt` | 57 passed, 2.03s | [E] |
| `05-smoke-cli.txt` | run->submit->resume->review 完整 | [E] |
| `06-check-auto.json` | auto 检查: all ok | [E] |
| `06-check-semi.json` | semi 检查: all ok | [E] |
| `06-check-agent.json` | agent 检查: ok (RDKit + MinerU) | [E] |
| `07-cli-driver.txt` | 首次驱动（paper.pdf）: partial, review=1 | [E] |
| `08-cli-driver-fixed.txt` | 修正驱动（paper-fixed.pdf）: 三模式 success | [E] |
| `09-repo-three-mode-runner.txt` | 三模式综合测试: 100% 契约 | [E] |
| `10-full-suite-latest.txt` | 最新套件: 184 passed, 85.14%, 5.54s | [E] |
| `10-ruff-latest.txt` | 最新 Ruff: passed | [E] |
| `10-pyright-latest.txt` | 最新 Pyright: 0 issues | [E] |
| `/tmp/opencode/report-build/packaging-latest.txt` | 当前 HEAD 的 build / twine check 均通过，RDKit=2025.09.3 | [E] |
| `/tmp/opencode/report-build/render-report-final.txt` | PDF 12 页、文本层检查和重复渲染通过 | [E] |
| `/tmp/opencode/report-build/artifacts.sha256` | Markdown/PDF SHA-256 | [E] |
| `cli-runs-fixed/auto/manifest.json` | auto manifest: all cli_model | [E] |
| `cli-runs-fixed/semi/manifest.json` | semi manifest: structure=host_agent | [E] |
| `cli-runs-fixed/agent/manifest.json` | agent manifest: all host_agent | [E] |
| `cli-runs-fixed/submission-semi.jsonl` | semi 提交: 2 条结构候选 | [E] |
| `cli-runs-fixed/submission-agent.jsonl` | agent 提交: text + table + structure | [E] |

## 12. 结论

ChemEx-Lit v1.0.0a1 的三模式运行时核心已通过本轮直接验证：

1. **auto 模式**：一条命令完成 document -> extraction -> validation -> assembly -> finalization 全阶段，产出 1 条记录，无需外部交互。
2. **semi 模式**：正确暂停于 structure 通道，宿主提交结构候选后恢复成功，验证了 `awaiting_input -> ready -> success` 状态机。
3. **agent 模式**：为 text/table/structure 三个通道独立生成 task，宿主提交后全部 accepted 并恢复成功。

代码质量门禁全部通过：184 个测试用例、85.14% 覆盖率、Ruff 零警告、Pyright 零错误。

当前未闭环的不是核心代码测试，而是两项外部集成：MinerU 云端凭证有效性（返回 401）和 LLM 模型实际调用（key 未设置）。这两项在测试套件中通过 mock 正确隔离，不影响对代码行为正确性的判断。

完成凭证配置后，对真实 Golden paper 的端到端验收将是下一步关键里程碑。
