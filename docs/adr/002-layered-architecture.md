# ADR-002：ChemEx 分层架构 —— CLI 执行内核 + 薄 Skill 编排 + 按需 MCP

| 字段 | 值 |
| --- | --- |
| 状态 | 已定稿（v3，经评审有条件通过后修订） |
| 日期 | 2026-07-17 |
| 取代 | v2 草案 `chemex-lit-auto-semi-mcp-skill-plan.html`（未提交） |
| 适用版本 | 自 1.0.0a1 起；当前处于可调整 `RunSummary` 状态枚举与任务契约的最后窗口 |

## 1. 结论

不把 ChemEx 改成"纯 Skill"。采用分层架构：

```text
ChemEx Core + CLI      稳定执行内核
        ↑
Agent Skill            默认 Agent 入口（薄编排）
        ↑
Optional MCP / Plugin  多轮交互与正式分发（按需延后）
```

- **CLI 保留**：负责 RDKit、MinerU、阶段缓存、Schema、验证、批处理和 CI。
- **Skill 做薄编排**：选择 auto/semi/agent 模式、调用 CLI、让宿主视觉生成任务提交、解释复核结果；绝不复制化学逻辑。
- **Semi 模式正式化**：新增 `awaiting_input` 状态与任务包；人工或 Agent 提交结构后继续同一验证链。
- **MCP 延后**：仅当状态查询、证据读取、结构回填等多轮需求被验证后，提供本地 stdio MCP。
- **不把 Gemini 网页 DOM 自动化作为主架构**；最多保留为独立实验适配器。
- 最终以 Plugin 打包 Skill 与可选 MCP。

依赖方向固定向下：`Skill / CLI / MCP → Application Service → Pipeline → Domain Models`。

## 2. 背景与现状（2026-07 代码事实）

现有 v1 core（`src/chemex_lit/`）：单一 `Pipeline`、CLI、稳定 `records.jsonl`（`schema_version: "1.0"`）、`ArtifactStore` 原子写与阶段哈希恢复、确定性 `Validator`/`Assembler`、acceptance-only `Adjudicator`、单 HTML review。

评审确认、必须在 P0A 修复的缺陷：

| 位置 | 缺陷 |
| --- | --- |
| `store.py` `initialise` | manifest 已存在时不比对新旧配置，resume 可能静默复用配置不符的产物 |
| `pipeline.py` extraction 指纹 | 含文档、提示词、人工结构，但不含实际模型配置 |
| `pipeline.py` assembly 指纹 | 只记录 adjudicator 有无，不含 reasoning 模型、裁决提示词、决策文件哈希 |
| `review.py` `apply_corrections` | 可改 reactants/products（即可改 SMILES），仅 Pydantic 校验，随后无条件 `accepted`，绕过 RDKit 重验证 |
| `mineru.py` | `page_idx=0` 被 `or` 吞掉；表格 `img_path` 被忽略且 `source_path` 指向 content-list JSON；`content_lists[0]` 任取文件；全目录 glob 使表格裁图混入 `StructureExtractor` 图像集 |
| `llm/client.py` | `temperature=0` 与 `response_format` 硬编码，reasoning 模型无法省略参数 |
| `models.py` `ReactionCandidate` | 要求提交方提供 `candidate_id`，外部提交无法绑定任务 |
| 默认配置 | `deepseek-chat` / `deepseek-reasoner` 官方计划于 2026-07-24 停用 |

## 3. 三档模型配置

```yaml
models:
  text:        # 正文/实验段反应描述 → ReactionCandidate
    base_url: https://api.deepseek.com/v1
    model: deepseek-v4-flash
    api_key_env: CHEMEX_TEXT_API_KEY
  vision:      # 表格图像 → ReactionCandidate；结构图像 → StructureCandidate
    base_url: https://open.bigmodel.cn/api/paas/v4
    model: glm-4v-plus
    api_key_env: CHEMEX_VISION_API_KEY
  reasoning:   # 裁决（证据约束的 accept/keep_review）
    base_url: https://api.deepseek.com/v1
    model: deepseek-v4-pro
    api_key_env: CHEMEX_REASONING_API_KEY
```

规则：

- 三档均为完整 `ModelSpec`，互相独立；允许指向同一端点同一模型，也允许完全不同供应商。
- `models.reasoning` 缺省时回退为 `models.text` 并记录 warning；manifest 如实记录实际使用模型。
- 新增环境变量覆盖：`CHEMEX_REASONING_BASE_URL / MODEL / API_KEY_ENV`。
- `ModelSpec` 改为显式省略语义：`temperature`、`max_tokens`、`response_format` 为 `None` 时不出现在请求体；新增 `extra_payload` 字典透传供应商特有参数（如 thinking 配置）。
- **v1 支持范围限定为 OpenAI-compatible Chat Completions 子集**；o-series / DeepSeek thinking 经 `extra_payload` 透传，不做一等字段，不宣称完整支持。

各档职责边界：

| 档位 | 唯一职责 | 不做什么 |
| --- | --- | --- |
| text | 正文文本 → `ReactionCandidate` | 不读图、不裁表格、不裁决 |
| vision | 表格图像 → `ReactionCandidate`；结构图像 → `StructureCandidate` | 不做正文抽取、不裁决 |
| reasoning | 仅对 warning-only 记录做证据约束的接受判断 | 永不改写化学内容 |

## 4. 模式 × 通道矩阵

| 通道 | Auto（纯 CLI） | Semi（纯 CLI 独立） | Agent-native（经 Skill） |
| --- | --- | --- | --- |
| PDF 解析 | MinerU 云服务 | MinerU 云服务 | MinerU 云服务 |
| text | cli_model（`models.text`） | cli_model（`models.text`） | host_agent |
| table | cli_model（`models.vision`，图像+文本） | cli_model（`models.vision`，图像+文本） | host_agent（视觉） |
| structure | cli_model（`models.vision`） | human 提交 | host_agent（视觉） |
| 验证 / 装配 | 确定性（RDKit / 规则） | 同左 | 同左 |
| adjudication | cli_model（`models.reasoning`） | cli_model（`models.reasoning`） | host_agent |
| finalization | 模板 + 确定性指标 | 同左 | 同左 |

每次 run 的通道指派实例化为 `producer_plan`，写入 manifest 并参与阶段指纹：

```yaml
producer_plan:
  text:         { kind: cli_model,  model: deepseek-v4-flash }
  table:        { kind: cli_model,  model: glm-4v-plus }
  structure:    { kind: human }
  adjudication: { kind: cli_model,  model: deepseek-v4-pro }
```

## 5. 关键设计

### 5.1 ProducerPlan 与 provenance（替代"单一来源"不变量）

**不采用**"单次 run 的 produced_by 必须单一"——它与 Auto 三档并用、Semi 人机混合、宿主内部委托的现实矛盾。替换为：

> 每个生成通道必须在 `producer_plan` 中显式声明，且在一次 run 中不得静默切换。

provenance 载体为 sidecar `provenance.jsonl`（与候选文件同目录）；**不给 Candidate 加 `produced_by` 默认值**（避免人工 JSONL 被错标为 `cli_model`）；`ReactionRecord` v1 schema 保持不变。

每条 provenance 至少包含：

```json
{
  "candidate_id": "cand-a1b2c3",
  "task_id": "st-0001",
  "channel": "structure",
  "producer_kind": "human | cli_model | host_agent",
  "provider": "<endpoint 标识, 可获得时>",
  "model": "glm-4v-plus",
  "prompt_version": "structure@3",
  "instruction_version": "chemex-instructions@1",
  "input_hash": "sha256:…",
  "submission_hash": "sha256:…",
  "client": { "name": "codex", "skill_version": "0.2.0" },
  "created_at": "…"
}
```

CLI 直跑通道同样写 provenance（provider/model/prompt_version 天然可得），三模式归因无差别。宿主内部分派（不同内部模型）对 Core 透明，Core 记录 `host_agent` 与 client 版本。

### 5.2 阶段拆分与缓存指纹

阶段边界重划（仍是单一 `Pipeline`）：

```text
document → extraction → validation → assembly → adjudication → finalization
                                                                    │
              adjudication: cli_model(reasoning) 或 awaiting_input ──┘
              assembly 完全确定性，不含任何模型因素
              finalization: review.html / review.jsonl / 终态计算
```

每阶段 fingerprint 必须包含：

| 成分 | 说明 |
| --- | --- |
| 非秘密 ModelSpec | 该阶段涉及通道的 base_url/model/max_tokens 等（不含 key） |
| producer_plan 切片 | 该阶段涉及通道的 kind/model |
| prompt / instruction version | `prompts.versions` 与任务包 `instruction_version` |
| 上游输入 artifact 哈希 | 沿用现有机制 |
| submission / provenance 哈希 | 外部履行阶段的提交内容哈希 |
| 工具版本 | `__version__`；validation 阶段含 `rdkit.__version__` |
| adjudication 决策哈希 | 外部决策文件哈希或 reasoning 模型标识 |

`ArtifactStore.initialise` 在 manifest 已存在时比对当前 `config_sha256` 与记录值；不一致则报错并列出变更键，禁止静默复用。更换模型后旧产物因指纹不匹配自然失效。

### 5.3 任务边界提交（ExtractionTask / CandidateSubmission）

外部提交不再直接提供 `candidate_id`，Core 收回 ID 控制权：

```json
// ExtractionTask（Core 生成，三模式统一抽象）
{
  "task_id": "st-0001",
  "kind": "text | table | structure | adjudication",
  "instruction_version": "chemex-instructions@1",
  "output_schema_version": "ReactionCandidate@1",
  "evidence_ids": ["ev-041"],
  "input_artifacts": ["document.json"],
  "assets": {
    "text": "<内联文本>",
    "images": [{ "evidence_id": "…", "path": "raw/…" }]
  },
  "status": "awaiting | fulfilled"
}

// CandidateSubmission（外部提交）
{
  "task_id": "st-0001",
  "producer": {
    "kind": "human | host_agent",
    "client": { "name": "…", "version": "…" }
  },
  "outputs": [ "…符合 output_schema 的负载，无 candidate_id…" ]
}
```

Core 收到提交后依次：① 验证 `task_id` 处于 awaiting；② 验证提交引用的 evidence 存在且属于该任务；③ 计算稳定 `candidate_id`（task_id + 输出内容哈希）；④ 写 provenance sidecar；⑤ 判定任务集是否全部满足。

**统一抽象**：Auto = Core 生成任务后由 CLI 模型进程内同步履行；Semi = structure 任务外部履行；Agent = 全部任务外部履行。三模式共用同一任务机制，差异只在履行者。现有 `--structures` 参数保留为 structure 任务人工提交的快捷方式，内部映射为同一 Submission 流程。

### 5.4 状态机

```text
running ──外部任务未满足──▶ awaiting_input ──submit(校验+持久化)──▶ ready ──resume──▶ running
   │                            │                                         ▲
   │                            └────── submit --resume（显式组合）────────┘
   ├─ 全部完成 ─▶ success / completed_empty / partial（沿用现有终态）
   ├─ 异常 ─▶ failed
   └─ 显式放弃 ─▶ cancelled（新增；永不因时间流逝自动转换）
```

- 终态保留 `success`，不引入 `succeeded`。
- `submit` 只校验与持久化；`resume` 才推进执行；禁止隐式双推进。
- `--force` 创建 `supersedes` 审计记录并失效全部下游阶段；Skill 永不自行使用。
- `RunSummary.status` 枚举扩为 `running | awaiting_input | ready | success | completed_empty | partial | failed | cancelled`（趁 1.0.0a1 窗口调整）。

### 5.5 裁决语义

外部裁决决策枚举为 **`accept` / `keep_review`**。不提供 `reject`：允许模型把记录标记为 rejected 需另行 ADR；最终拒绝保留为人工复核动作。维持 "acceptance only" 不变量——裁决永不改写化学字段。

### 5.6 Correction 通道（suggest → confirm → revalidate）

废除"correction 后无条件 accepted"。新流程：

```text
Skill/人工产出 correction 建议文件
  → chemex-lit review-apply RUN_DIR corr.json --confirmed-by "<确认人>"
  → Schema 校验 + 应用字段修改
  → 对受影响记录重新执行 RDKit Validator（不可跳过）
  → 按 assembly 规则重算 issues 与 review_status（error 仍在则维持 needs_review）
  → 写 records.corrected.jsonl + 审计条目（确认人、时间、改动、验证前后状态）
```

- Skill 只能建议；`--confirmed-by` 为必填的人工确认身份，Skill 不得自行填充。
- 修正后仍含 error 的记录不得转为 accepted。

### 5.7 MinerU 表格资产

| 项 | 决定 |
| --- | --- |
| content-list 选择 | 显式按命名/版本字段选择；多候选时报错而非任取第一个 |
| `img_path` | 解析并持久化为 `EvidenceRef.asset_path`（新增可选字段）；`source_path` 保留指向 content-list 作为出处，两字段语义分离 |
| 页码 | `page_idx if page_idx is not None else page`；记录页码基准（MinerU `page_idx` 为 0 基），统一归一化 |
| bbox | 原样保存并标注坐标系（如 `bbox_space`）；不做隐式换算 |
| 图像交叉污染 | `StructureExtractor` 只消费未被任何 `kind="table"` 证据的 `asset_path` 引用的图像 |

MinerU content-list 官方格式自带表格 `img_path`、`bbox`、`page_idx`，**默认不使用** PyMuPDF 二次裁剪；`img_path` 缺失时的降级方案另行决策。

### 5.8 Reasoning 档与 LLM 客户端

- `ModelSpec` 显式省略语义见 §3；`LLMClient` 按 spec 组装 payload，不再硬编码 `temperature` / `response_format`。
- DeepSeek 迁移在 P0A 立即执行：默认与示例改用 `deepseek-v4-flash`（text）与 `deepseek-v4-pro`（reasoning，thinking 配置经 `extra_payload`）。

### 5.9 Agent 任务包与 P2 验收

- 文本任务**内联实际文本资产**，不使用 `section_path` 之类不稳定引用。
- `instructions` 与 `instruction_version` 由 Core 生成并随任务包下发，Skill 原样呈现——CLI 与 Agent 提取规则同源，杜绝漂移；Skill 不复制 prompt，也不只给裸 schema。
- 本地 `image_path` 对云端 Agent 未必可见，P2 双客户端验收必须写明：
  1. 哪两个客户端（如 Codex + 另一指定 Agent Skills 客户端）；
  2. 各客户端的图像传递机制（文件附加 / 工作区挂载 / base64，按客户端能力）；
  3. 单批最大任务数（超出分批 submit）；
  4. 中断后按 `task_id` 恢复（部分提交合法，剩余任务保持 awaiting）。

## 6. 信任边界与不变量

保留的现有不变量：单一 `Pipeline`；公开记录经 `chemex_lit.models` 校验；验证器报告问题、不做生成修复；装配确定性；裁决 acceptance-only 且不改写化学；运行时资源走 `importlib.resources`；凭证仅来自环境变量；一切写入过 `ArtifactStore`；`completed_empty ≠ success`。

修订的不变量：

- 人工结构以**任务边界 Submission** 进入（`--structures` 为其快捷方式）；
- 提取器重定义为"任务履行者"，可在进程内（CLI 模型）或外部（human/host）。

新增不变量：

- 每通道 producer 显式声明、不静默切换；
- `candidate_id` 一律由 Core 计算；
- 无时间驱动的状态衰减，放弃即显式 `cancelled`；
- `ReactionRecord` v1 schema 不变，provenance 走 sidecar；
- Skill/人工只能到达 candidates / adjudication decisions / 经确认的 corrections，永远不能直达 `records.jsonl`。

## 7. 实施顺序

| 阶段 | 内容 |
| --- | --- |
| **P0A** | 阶段指纹全覆盖 + resume 配置比对；correction 重验证改造；DeepSeek V4 别名与 `ModelSpec` 显式省略；`page_idx` / content-list 选择 / 表格图混入结构提取三处 MinerU 修复 |
| **P0B** | 数据契约：`ProducerPlan`、`ExtractionTask`、`CandidateSubmission`、`RunState`、provenance sidecar schema；状态枚举扩展 |
| **P1** | assembly/adjudication 拆分；`application/service.py`；Semi 模式；`submit`/`resume`/`status` 语义落地 |
| **P1.5** | MinerU 表格资产完整解析（img_path/asset_path/坐标系）→ 表格提取切换 vision → 新旧路径基准对照，不劣化才转正 |
| **P2** | Agent-native（任务包全外部化 + 裁决外部化）+ 薄 Skill；按 §5.9 四项标准做双客户端 golden-paper 验收 |
| **P3** | 有真实多轮需求后：本地 stdio MCP + Plugin 打包 |

## 8. 发布门槛

1. CLI 与 MCP 对同一输入产出相同 `records.jsonl`。
2. 三模式从 validation 起同码；任务抽象三模式统一。
3. Skill 只能提交：任务边界 Submission、`accept`/`keep_review` 裁决、经 `--confirmed-by` 确认的 correction 建议。
4. 全部写操作幂等；带 run_id、输入哈希、submission 哈希、provenance；`--force` 留 supersedes 审计并失效下游。
5. 无 Agent/MCP 时 CLI 独立可用；三档模型可指向同一端点；reasoning 缺省回退 text 并告警。
6. Skill 在两个指定客户端以明确的图像传递机制通过同一 golden-paper；支持 task_id 断点恢复。
7. 表格视觉路径对文本路径基准不劣化。
8. 变更任一模型/prompt/producer 仅失效受影响阶段；resume 绝不静默复用配置不符的产物。
9. correction 经重验证后不得无条件 accepted，审计含确认人。

## 9. 延后与拒绝

| 项 | 决定 | 理由 |
| --- | --- | --- |
| Gemini 网页 DOM 自动化 | 拒绝为主线，最多独立实验适配器 | 脆弱、难审计、受登录与页面变化影响 |
| 纯 Skill 架构 | 拒绝 | 无可复现执行内核，受宿主行为漂移影响 |
| MCP 立即实现 | 延后至 P3 | 多轮回填与状态查询需求未验证 |
| 裁决 `reject` 语义 | 拒绝，除非另行 ADR | 最终拒绝应是人工复核动作 |
| PyMuPDF 二次裁剪表格 | 默认不使用 | MinerU 官方格式已提供 img_path/bbox/page_idx |
| "单一来源"不变量 | 拒绝 | 与三档并用、人机混合现实矛盾；以 ProducerPlan 显式声明替代 |

## 10. 参考

- OpenAI Skills / OpenAI Plugins 官方文档
- Agent Skills specification（agentskills.io）
- MCP architecture（modelcontextprotocol.io）
- MinerU 输出格式官方文档
- DeepSeek 官方模型变更说明（2026-07-24 停用旧别名）
