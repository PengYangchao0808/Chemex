# ChemEx-Lit 开发报告

## 1. 报告信息

| 项目 | 内容 |
| --- | --- |
| 项目 | ChemEx-Lit |
| 当前版本 | `1.0.0a1` |
| 报告日期 | 2026-08-02 |
| 代码形态 | Python 3.11+，`src` layout |
| 核心目标 | 从化学文献 PDF 中提取可追溯、可验证的反应记录 |
| 当前状态 | 核心改造、自动化验证和打包已完成；五模式真实论文测试受 MinerU 凭证阻塞 |
| Golden paper | Xiong et al. (2003) stereoselective intramolecular 4+3 cycloadditions |

## 2. 执行摘要

ChemEx-Lit 已形成“稳定 CLI/Core + 薄 Agent Skill 编排”的分层架构。CLI/Core 负责 MinerU 文档解析、任务生成、化学候选验证、确定性装配、持久化、断点恢复和审计；宿主 Agent 只在外部任务边界内完成生成或人工协作，不直接修改最终记录。

本轮开发的核心成果是把原有三模式扩展为五种可组合的执行模式，并建立独立的模型 Profile 系统：

1. 模型 endpoint、模型名和非秘密参数进入 `models.yaml` Profile。
2. Secret 仍只通过环境变量提供，YAML 不保存密钥值。
3. `--profile`、`--models-path` 和 `models list/show/check` 支持显式选择和检查模型配置。
4. 新增 `human-ocsr-agent`，将 OCSR 以人工/Gemini Web 辅助方式保留在宿主流程之外，其余生成通道交给宿主。
5. 新增 `auto-agent`，允许宿主按 text/table/structure/adjudication 通道使用不同的 Codex/OpenCode 模型和 fallback 策略。
6. Profile、模型路由、producer 和 provenance 信息进入 manifest/fingerprint，避免不同配置复用旧产物。

自动化验证已经通过：137 个 pytest 用例、84.27% 覆盖率、Ruff、Pyright、源码构建、Twine 元数据校验和 clean wheel CLI 冒烟测试均通过。

但是，Xiong 论文的五次真实运行目前只完成了“命令实际发起并到达 MinerU 边界”的验证。当前 WSL 环境没有 `MINERU_API_KEY`，五次运行均在 MinerU 转换前停止，尚未形成新的五模式端到端成功产物。

## 3. 产品定位和使用边界

### 3.1 产品定位

ChemEx-Lit 不是一个把所有化学逻辑交给 Agent 的 Skill，也不是一个只负责调用大模型的网关。它是一个拥有稳定数据契约和可恢复执行内核的化学文献抽取系统：

```text
PDF
  -> MinerU document/evidence
  -> task-bounded extraction
  -> validation
  -> deterministic assembly
  -> optional adjudication
  -> final records/review
```

### 3.2 责任边界

| 组件 | 负责内容 | 明确不负责 |
| --- | --- | --- |
| `Pipeline` | 协调阶段、状态和输入输出 | 不复制第二套自动/人工编排器 |
| MinerU adapter | PDF 解析、文档和证据资产落盘 | 不做化学推理 |
| Extractors | 选择证据、调用模型或生成任务、返回候选 | 不直接写最终 records |
| Validator | RDKit 解析、规范化和问题报告 | 不生成性修复化学结构 |
| Assembler | 确定性合并候选和生成记录 | 不使用模型裁决化学字段 |
| Adjudicator | 对 warning-only 候选做 `accept`/`keep_review` 判断 | 不改写化学内容，不提供 `reject` |
| ArtifactStore | 原子写入、hash、manifest、resume | 不允许绕过持久化边界 |
| Skill/宿主 Agent | 读取任务、完成外部任务、提交 JSONL、复核 | 不编辑 `records.jsonl` |

该边界符合 ADR-002：MCP 和 Plugin 不作为当前基础架构的前置依赖，待多轮真实需求验证后再进入 P3。

## 4. 当前架构

### 4.1 生产目录

```text
src/chemex_lit/
├── models.py              # 稳定 v1 数据契约
├── pipeline.py            # 唯一生产 Pipeline
├── store.py               # ArtifactStore、原子产物和 resume
├── mineru.py              # MinerU 云端适配器
├── extraction/            # text/table/structure/task 生成
├── llm/                   # OpenAI-compatible client 和 prompt registry
├── chemistry/             # RDKit 验证和渲染
├── assembly.py            # 确定性候选装配
├── adjudicator.py         # acceptance-only 裁决
├── review.py              # review 生成和人工确认修订
├── evaluation/            # 确定性评估指标
└── resources/             # 默认配置、Profile、prompt 和模板
```

### 4.2 阶段和产物

每次运行都使用独立的 run directory，典型结构为：

```text
<run-dir>/
├── manifest.json
├── document.json
├── evidence.jsonl
├── audit.jsonl
├── tasks/
│   ├── extraction.jsonl
│   ├── adjudication.jsonl
│   └── state.json
├── candidates/
│   ├── reactions.jsonl
│   ├── structures.jsonl
│   └── provenance.jsonl
├── validation/
│   ├── outcome.json
│   └── issues.jsonl
├── assembly/
│   └── records.jsonl
├── records.jsonl
├── review.jsonl
├── review.html
└── raw/mineru/
```

`records.jsonl` 是最终公共产物，`assembly/records.jsonl` 是裁决前记录集。任务提交、候选、验证、装配和最终记录之间通过 evidence、task、submission hash 和 provenance 关联。

### 4.3 状态机

```text
running
   -> awaiting_input --submit--> ready --resume--> running
   -> success
   -> completed_empty
   -> partial
   -> failed
   -> cancelled
```

`submit` 只负责校验和持久化，`resume` 才推进下一阶段。外部任务未完成时保持 `awaiting_input`；空抽取使用 `completed_empty`，不使用 `success` 伪装。

## 5. Model Profile 系统

### 5.1 设计目标

旧设计通过一组 `CHEMEX_*_MODEL` 环境变量选择模型，不适合多模型 benchmark，也无法清楚记录 endpoint、参数和实际 producer。当前设计将配置分成两层：

- Profile YAML：保存 endpoint、模型名、参数、API key 环境变量名和宿主路由策略。
- Environment：只保存 API key 实际值。

默认用户配置位置由 `platformdirs` 决定：

- Linux：`~/.config/chemex-lit/models.yaml`
- macOS：`~/Library/Application Support/chemex-lit/models.yaml`
- Windows：`%LOCALAPPDATA%\chemex-lit\models.yaml`

仓库内的 [default_models.yaml](../src/chemex_lit/resources/default_models.yaml) 提供可打包的 `deepseek-glm` 和 `openai` 示例 Profile。

### 5.2 选择优先级

```text
CLI --profile
    > models.yaml default_profile
    > 默认配置中的 legacy models
```

显式 `--models-path` 可指定另一份 Profile 文件。Profile 激活时，如果仍设置了 `CHEMEX_*_MODEL` 调试环境变量，CLI 会发出 warning，防止 benchmark 产生隐式覆盖。

### 5.3 Profile 结构

模型配置示例：

```yaml
version: 1
default_profile: deepseek-glm

profiles:
  deepseek-glm:
    text:
      base_url: https://api.deepseek.com/v1
      model: deepseek-v4-flash
      api_key_env: DEEPSEEK_API_KEY
    vision:
      base_url: https://open.bigmodel.cn/api/paas/v4
      model: glm-4v-plus
      api_key_env: GLM_API_KEY
    reasoning:
      base_url: https://api.deepseek.com/v1
      model: deepseek-v4-pro
      api_key_env: DEEPSEEK_API_KEY

  codex-diverse:
    host_routes:
      text:
        policy: codex-text
        model: gpt-5
        fallbacks: [gpt-5-mini]
      table:
        policy: codex-table
        model: gpt-5-mini
      structure:
        policy: gemini-web-ocsr
        model: human-assisted
      adjudication:
        policy: codex-review
        model: gpt-5
```

`host_routes` 是宿主路由元数据，不是 CLI 直接调用的模型网关。CLI 将 policy/model/fallbacks 写入 producer plan 和 provenance，宿主 Agent 负责真正执行。

### 5.4 CLI

```bash
chemex-lit models list
chemex-lit models show deepseek-glm
chemex-lit models check deepseek-glm
chemex-lit --profile deepseek-glm check --mode auto
chemex-lit --models-path configs/models.yaml --profile codex-diverse check --mode auto-agent
```

`models show` 只显示非秘密配置；`models check` 检查引用的环境变量是否存在，不打印密钥值。

## 6. 五种运行模式

### 6.1 总体矩阵

| 模式 | Text | Table | Structure/OCSR | Adjudication | CLI 侧主要凭证 |
| --- | --- | --- | --- | --- | --- |
| `auto` | CLI text model | CLI vision model | CLI vision model | CLI reasoning model | MinerU + text + vision；reasoning 按配置回退 |
| `semi` | CLI text model | CLI vision model | 人工/外部提交 | CLI reasoning model | MinerU + text + vision |
| `agent` | 宿主 Agent | 宿主 Agent | 宿主 Agent | 宿主 Agent | CLI 只需 MinerU |
| `human-ocsr-agent` | 宿主 Agent | 宿主 Agent | 人工或 Gemini Web 辅助 OCSR | 宿主 Agent | CLI 只需 MinerU |
| `auto-agent` | 宿主路由 | 宿主路由 | 宿主路由 | 宿主路由 | CLI 只需 MinerU |

### 6.2 模式语义

- `auto`：适合 CI、批处理和可复现 benchmark，所有生成通道由 CLI 内部模型完成。
- `semi`：只把 structure/OCSR 外置，适合人工提供 SMILES 或人工核对结构。
- `agent`：所有生成性任务都变成宿主可提交的 task，适合使用 Codex/OpenCode 的视觉和推理能力。
- `human-ocsr-agent`：这是当前“除 OCSR 外全部走宿主”的半自动模式。text/table/adjudication 交给宿主；OCSR 必须由人工完成，Gemini Web 只能作为人工辅助工具。
- `auto-agent`：宿主接管所有 channel，并依据 `host_routes` 选择不同模型或 fallback。CLI 不主动调用 Codex 模型。

需要特别区分：当前实现的 `auto-agent` 也会为 structure 生成宿主任务；如果产品要求 OCSR 在所有宿主模式都必须由人工确认，应在下一轮将 `auto-agent` 的 structure route 固化为 human gate，而不是自动模型 route。

### 6.3 宿主工作流

```bash
chemex-lit --profile codex-diverse run paper.pdf \
  --mode auto-agent --adjudicate \
  --output-dir outputs/paper-auto-agent

chemex-lit status outputs/paper-auto-agent
# 读取 tasks/extraction.jsonl，按 task_id 生成 task-bounded JSONL
chemex-lit submit outputs/paper-auto-agent submissions.jsonl
chemex-lit resume outputs/paper-auto-agent
```

宿主提交必须遵守：每行一个 task、不得填写 `candidate_id`、证据 ID 必须来自任务、裁决只能是 `accept` 或 `keep_review`，不得直接编辑 `records.jsonl`。

## 7. 可追溯性和安全不变量

当前实现保留以下强约束：

1. 所有公共记录通过 `chemex_lit.models` 校验。
2. 所有持久化写入经过 `ArtifactStore`。
3. `candidate_id` 由 Core 根据 task 和输出内容计算，外部不得伪造。
4. `records.jsonl` 不允许人工直接编辑。
5. `--force` 不属于标准工作流。
6. `--confirmed-by` 必须是真实人工确认者，由宿主向用户询问。
7. Validator 报告问题但不做生成性化学修复。
8. Assembler 是确定性的，Adjudicator 不能重写化学字段。
9. 凭证只来自环境变量，永不写入 Profile YAML、manifest 或 provenance。
10. Profile 名称、models source、非秘密模型配置、producer plan 和阶段指纹写入 manifest。

当模型、prompt、producer、输入 PDF、提交内容或工具版本变化时，相关阶段 fingerprint 变化；resume 不会静默复用不兼容的旧产物。

## 8. 本轮代码变更

### 8.1 新增文件

- `src/chemex_lit/profiles.py`：ProfileSpec、Profile、ProfilesFile、宿主路由定义和加载选择逻辑。
- `src/chemex_lit/resources/default_models.yaml`：打包的 `deepseek-glm`、`openai` 示例 Profile。
- `tests/test_profiles.py`：Profile 加载、优先级、环境变量 warning、manifest 字段测试。
- `tests/test_cli_profile.py`：Profile CLI、显式 models path 和五模式 help 测试。
- `tests/test_routing_modes.py`：五模式 ProducerPlan、宿主路由和 producer metadata 测试。
- `docs/chemex-development-report.md`：本报告。

### 8.2 修改文件

- `src/chemex_lit/config.py`：Profile 注入、models source、host routes 和环境变量覆盖 warning。
- `src/chemex_lit/cli.py`：根级 `--profile`/`--models-path`、models 子命令和五模式选项。
- `src/chemex_lit/pipeline.py`：五模式 producer plan、宿主 policy/model/fallback 和 provenance。
- `src/chemex_lit/models.py`：五模式类型、host producer metadata 和 attempt metadata。
- `src/chemex_lit/store.py`：manifest profile/source 记录及 resume 时配置变化检查。
- `src/chemex_lit/application/service.py`：五模式服务层白名单。
- `README.md`：Profile、benchmark、五模式和 Codex/OpenCode 路由示例。
- `chemex-lit-skill/`：仓库内 Skill 和契约文档更新为五模式。
- `pyproject.toml`：新增 `platformdirs>=3.0`。

## 9. 测试和交付验证

### 9.1 自动化测试

最近一次完整验证结果：

```text
pytest:       137 passed
coverage:     84.27% (required 70%)
ruff:         All checks passed
pyright:      0 errors, 0 warnings, 0 informations
```

测试覆盖范围包括：

- 旧版配置和 Profile 配置兼容性。
- `--profile`、`--models-path`、`models list/show/check`。
- 五种模式的 producer plan 和 host route metadata。
- 人工 OCSR 与宿主 Agent 的暂停、提交和 resume 语义。
- manifest/profile/source/fingerprint 和配置变更保护。
- MinerU、LLM、结构抽取、验证、装配、review、store 和 service。

### 9.2 打包验证

- `python -m build`：sdist 和 wheel 均构建成功。
- `twine check`：wheel 与 sdist 均通过。
- clean virtual environment 安装 wheel 成功。
- 安装后的 CLI `--version`、`models list` 和 `run --help` 成功。
- `default_models.yaml` 正确包含在 wheel 和 sdist 中。

### 9.3 测试边界

测试套件不调用真实 MinerU 或 LLM，这是有意设计：外部服务通过 mock 隔离，确保单元和契约测试可重复。真实服务验证必须单独执行，并且必须保存 run directory、manifest、provenance 和服务配置摘要。

## 10. Xiong 论文真实测试状态

### 10.1 输入

仓库中已有 PDF：

```text
xiong-et-al-2003-stereoselective-intramolecular-4-3-cycloadditions-of-nitrogen-stabilized-chiral-oxyallyl-cations-via.pdf
```

### 10.2 五模式运行结果

五种命令均已实际发起，但全部在 `MinerUAdapter.convert` 之前停止：

| 模式 | 是否发起 | 当前结果 | 根因 |
| --- | --- | --- | --- |
| `auto` | 是 | 未完成 | `MINERU_API_KEY` 未设置 |
| `semi` | 是 | 未完成 | `MINERU_API_KEY` 未设置 |
| `agent` | 是 | 未完成 | `MINERU_API_KEY` 未设置 |
| `human-ocsr-agent` | 是 | 未完成 | `MINERU_API_KEY` 未设置 |
| `auto-agent` | 是 | 未完成 | `MINERU_API_KEY` 未设置 |

验证命令当前返回：

```text
OK      RDKit: import and parse
MISSING MinerU key: MINERU_API_KEY
Profile: deepseek-glm
Source:  profile:deepseek-glm
Mode:    agent
```

因此，本报告不把五次运行标记为成功，也不把历史的 `outputs/xiong_2003_agent_20260718_v2` partial 产物当作本轮五模式验收结果。历史产物只能证明过去曾经完成过一次 agent 方向的部分运行，不能证明当前 Profile 和五模式实现已经通过真实 Golden paper 验收。

### 10.3 完成真实验收所需命令

先在实际运行 ChemEx 的 WSL 环境设置凭证：

```bash
export MINERU_API_KEY="..."
export DEEPSEEK_API_KEY="..."
export GLM_API_KEY="..."
export OPENAI_API_KEY="..."
```

然后检查 CLI 模式：

```bash
chemex-lit --profile deepseek-glm check --mode auto
chemex-lit --profile deepseek-glm check --mode semi
chemex-lit --profile codex-diverse check --mode agent
chemex-lit --profile codex-diverse check --mode human-ocsr-agent
chemex-lit --profile codex-diverse check --mode auto-agent
```

五次运行必须使用独立输出目录：

```bash
PDF="xiong-et-al-2003-stereoselective-intramolecular-4-3-cycloadditions-of-nitrogen-stabilized-chiral-oxyallyl-cations-via.pdf"

chemex-lit --profile deepseek-glm run "$PDF" --mode auto --adjudicate \
  --output-dir outputs/xiong-real/auto

chemex-lit --profile deepseek-glm run "$PDF" --mode semi --adjudicate \
  --output-dir outputs/xiong-real/semi

chemex-lit --profile codex-diverse run "$PDF" --mode agent --adjudicate \
  --output-dir outputs/xiong-real/agent

chemex-lit --profile codex-diverse run "$PDF" --mode human-ocsr-agent --adjudicate \
  --output-dir outputs/xiong-real/human-ocsr-agent

chemex-lit --profile codex-diverse run "$PDF" --mode auto-agent --adjudicate \
  --output-dir outputs/xiong-real/auto-agent
```

验收不应只看命令退出码，还应检查：

1. 每个 run 的 manifest 中 profile、models source 和 producer plan 正确。
2. `semi` 和 `human-ocsr-agent` 的 structure 任务确实经过人工/Gemini Web 辅助提交。
3. `agent` 和 `auto-agent` 的 text/table/structure/adjudication 任务均按 task_id 提交。
4. `auto-agent` 的 provenance 记录宿主 policy、model 和 attempt。
5. 五个 run 都生成 validation、assembly、records、review 和 provenance 产物。
6. 使用 `chemex-lit evaluate` 对照 gold JSONL 计算精确率、召回率和结构匹配指标。

## 11. 当前风险和待办

### P0：完成 Golden paper 真实验收

当前最大阻塞是 WSL 会话缺少 `MINERU_API_KEY`。补齐凭证后，需要实际完成五模式运行，并记录每个模式的任务数量、提交次数、候选数、验证问题、最终 records 数量和 review 状态。

### P1：同步用户级 ChemEx Skill

仓库内 [chemex-lit-skill/SKILL.md](../chemex-lit-skill/SKILL.md) 已更新为五模式，但当前 Codex 用户级安装的 `C:\Users\彭扬超\.codex\skills\chemex-lit\SKILL.md` 仍是三模式说明。建议将仓库 Skill 安装/同步到宿主后重新打开会话，并验证宿主是否识别 `human-ocsr-agent` 和 `auto-agent`。

### P1：明确 auto-agent 的 OCSR 政策

当前 `auto-agent` 可以把 structure 任务交给宿主路由。若最终产品原则是“OCSR 永远必须人工确认”，应把 auto-agent 的 structure channel 改成强制 human gate，并在测试、Skill 和 manifest 契约中固定这一点。当前实现的人工 OCSR 语义明确落在 `human-ocsr-agent`。

### P2：建立可重复 benchmark 报告

Profile benchmark 需要统一：

- 同一 PDF 和同一输入 hash。
- 独立 output directory。
- 同一 prompt/instruction 版本。
- 每个 channel 的实际 model/provider/policy。
- token、耗时、重试和外部服务错误。
- chemical record precision/recall、SMILES canonical match 和 needs_review 比例。

建议在真实五模式测试完成后新增 benchmark 汇总脚本或 JSON 报告，而不是只用 shell loop 比较目录名称。

### P3：MCP/Plugin

当前不建议立即自建 MCP。CLI 已经支持核心执行和 resume；只有在多轮需求证明“状态查询、证据读取、任务提交”需要高频交互时，才进入本地 stdio MCP 和 Plugin 打包。

### P3：清理和版本化

当前工作树中仍存在未提交的历史修改和实验产物，例如 PDF、`submissions/`、旧 HTML 计划文件以及历史 outputs。正式发布前应单独决定：哪些进入版本库、哪些进入 `.gitignore`、哪些保留为验收附件。

## 12. 推荐下一阶段执行顺序

```text
1. 同步仓库 Skill 到 Codex/OpenCode 宿主
2. 在 WSL 配置 MINERU 和所需 Profile 的 API keys
3. 增加 codex-diverse Profile，并运行五模式 check
4. 对 Xiong PDF 完成五个独立 run
5. 对 host/human 模式完成 submit/resume 闭环
6. 保存 manifest/provenance/review 和服务日志
7. 使用 gold JSONL 执行 evaluate
8. 汇总 benchmark 和化学质量报告
9. 根据 OCSR 人工 gate 决策修订 auto-agent
10. 真实多轮需求稳定后再评估 MCP/Plugin
```

## 13. 结论

ChemEx-Lit 的核心工程基础已经从“单一 CLI + 三种执行模式”推进到“Profile 驱动、五模式、宿主可编排、可审计和可恢复”的 v1 形态。代码层、契约层、测试层和打包层已经具备继续做真实数据验收的条件。

当前尚未闭环的不是核心代码测试，而是两项外部集成验收：

1. MinerU 和模型凭证尚未在当前 WSL 会话提供，导致 Xiong 五模式真实测试尚未完成。
2. 仓库内五模式 Skill 与用户级已安装 Skill 尚未同步，存在宿主行为漂移风险。

完成凭证配置、Skill 同步和五模式 Golden paper 验收后，ChemEx-Lit 才能从“工程验证通过”升级为“真实化学文献流程验收通过”。
