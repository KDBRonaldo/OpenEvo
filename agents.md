# Polar 开发指南

本文件给 Codex/开发者提供仓库级协作约定。开始改代码前先读本文件，再按相关模块
README 和 `docs/architecture/` 补上下文。

## 项目定位

Polar 是面向真实 agent harness 的 rollout、gateway 和 evolution backend 系统。它的
核心目标是把 agent 运行过程稳定地转成可训练、可评估、可演化的数据，并把演化后的
memory、skill、agent system 和 parametric memory 注入后续 session。

当前系统有两条明确的数据路径：

- Token-level training path：agent 经过 Polar LLM proxy，Polar 捕获 completion
  records，并构造包含 token ids、loss mask、logprobs 的 trajectory。
- Pure-text evolution path：agent 不一定经过 Polar proxy，Polar 或外部 harness 只要求
  提供可解析的 text/transcript trajectory，用于 skill、memory、agent-system 等文本
  自进化。

不要把 subscription auth 和 capture mode 混为一谈。Subscription 只是某些 harness 的
认证/运行方式；pure-text evolution mode 才是订阅模式可用的数据形态。订阅模式必须显式
设置 `agent.settings.capture_mode="transcript"` 或等价 transcript capture mode 才允许生效。
Codex subscription 只是第一个落地 case，Claude Code、Gemini CLI、OpenHands 或其他
harness 只要能提供稳定 transcript，都应能接入 pure-text evolution。

## 目录结构

- `src/polar/`: Polar 主包。
- `src/polar/agent/`: agent harness contract 和 Codex、Claude Code、OpenHands 等 presets。
- `src/polar/gateway/`: gateway server、LLM proxy、runtime lifecycle、completion capture、
  evolution context 注入。
- `src/polar/trajectory/`: completion/session/transcript 到 `Trajectory` 的 builder 和
  evaluator。
- `src/polar/runtime/`: Docker/Apptainer runtime 抽象。
- `src/polar/rollout/`: rollout server、pipeline、balancer、result aggregation。
- `src/polar_evolution/`: Evolution Backend，包含 events、datasets、jobs、workers、
  artifacts 和 context resolver。
- `docs/architecture/`: 系统架构、evolution API、runtime context、worker 接口文档。
- `tests/`: regression tests，通常按 gateway、trajectory、evolution、rollout 等行为组织。

更多模块边界见各目录下的 `README.md`。

## Evolution 接口约定

Evolution Backend 不关心具体算法内部怎么实现 reflection、clustering、prompt
optimization、skill synthesis、LoRA training 或评估。算法只需要遵守统一边界：

- 输入边界：通过 datasets、input artifacts 和 `job.config` 获取数据和配置。
- 输出边界：返回一个或多个 `ArtifactRegisterRequest`，并提供 `type`、`uri`、
  `manifest`、`lineage`、`compatibility`、`scores`、`tags` 和 `promoted`。

通用数据流：

```text
session/task events -> dataset artifact -> evolution job -> worker method
  -> typed artifacts -> context resolve -> gateway runtime injection
```

### `text_memory`

用途：自然语言长期记忆。

消费：

- `dataset` artifact，通常包含 `manifest.json` 和 `records.jsonl`。
- Dataset records 可以来自 token-level proxy trajectory，也可以来自 pure-text transcript
  trajectory。
- 可选旧 memory artifacts 和 method-specific `job.config`。

产出：

- `ArtifactRegisterRequest(type=text_memory)`。
- `uri=file://.../memory.md`。
- `manifest` 中记录 source dataset、record count、content path 等。

Runtime 消费：

- Gateway 写入 `/polar/session/evolution/memory.md`。
- 设置 `POLAR_MEMORY_FILE`。
- 将 rendered memory prepend 到 agent instruction。

### `skill_bundle`

用途：agent harness 可加载的 skill bundle。

消费：

- Dataset/transcript records、旧 skill artifacts、manual config 或 research worker 自己的
  synthesis 输入。
- Reference method 当前可直接消费 `job.config.skill_markdown` 或 `job.config.content`。

产出：

- `ArtifactRegisterRequest(type=skill_bundle)`。
- `uri=file://.../<skill-dir>`。
- 目录中至少包含 `SKILL.md`，可包含辅助文件。

Runtime 消费：

- Gateway stage 到 `/polar/session/evolution/skills/`。
- Copy-based harness 通过 `POLAR_SKILLS_DIR` 加载。
- OpenHands 等 path-based harness 应让 evolution skill path 优先于静态 skill path。

### `agent_system`

用途：演化 agent system prompt 或 harness-specific instruction 文件。

消费：

- Dataset/transcript records、旧 agent-system artifacts、manual config 或 research worker
  生成的文本。
- `job.config.target_path` 可指定写入目标，默认 `AGENTS.md`。

产出：

- `ArtifactRegisterRequest(type=agent_system)`。
- `uri=file://.../<target_path>`。
- `manifest.target_path` 必须是安全的相对 harness instruction path。

当前允许的 target path：

- `AGENTS.md`
- `agents.md`
- `CLAUDE.md`
- `GEMINI.md`
- `.openhands/microagents/*.md`

Runtime 消费：

- Gateway 写入 canonical `/polar/session/evolution/agent_system.md`。
- Gateway 尝试写入 runtime workdir 下的 `target_path`。
- 设置 `POLAR_AGENT_SYSTEM_FILE`、`POLAR_AGENT_SYSTEM_TARGET` 和
  `POLAR_AGENT_SYSTEM_TARGETS`。
- 将 rendered agent-system text prepend 到 agent instruction。

### `parametric_memory`

用途：参数化长期记忆，例如 LoRA/adapter。

消费：

- 已训练 adapter 的 URI，或由专用 trainer worker 产生的 adapter artifact。
- `base_model`、`adapter_id`、`adapter_format`、scores 和 compatibility。

产出：

- `ArtifactRegisterRequest(type=parametric_memory)`。
- `manifest.adapter_id`、`manifest.base_model`、`manifest.adapter_format`。
- URI 指向 adapter 目录或远端引用。

Runtime 消费：

- Context resolver 将其转成 `adapter_merge_spec`。
- Gateway/proxy 当前执行 request-level LoRA selection。
- 当前不在 gateway 内做物理权重 merge；serving backend 必须已经加载对应 adapter，或由
  后续 dynamic adapter loading 路径处理。

## Capture 和 Trajectory 约定

### Token-level capture mode

入口：

- Agent 通过 Polar LLM proxy 调 OpenAI/Anthropic/Google compatible APIs。

产物：

- `CompletionSession`。
- Token-level `Trajectory`。
- `Trace.response_ids`、`Trace.loss_mask`、`Trace.response_logprobs` 可用。

用途：

- Token-level RL。
- Policy gradient。
- 需要 sampled-token logprob 或 loss mask 的训练。

限制：

- 必须走 Polar proxy。
- 如果 harness 使用外部订阅直连模型服务，则不能声称有 token-level metric。

### Pure-text evolution mode

入口：

- Harness stdout/stderr transcript。
- Agent run transcript。
- 外部 text trajectory。
- 不要求经过 Polar proxy。

产物：

- Text/transcript trajectory。
- 至少保留 `prompt_messages`、`response_messages` 和必要 raw transcript/text metadata。
- 明确设置 `capture_mode="transcript"` 或等价 text capture 标记。
- 明确设置 `token_level_metrics_available=false`。
- 当前 gateway 的自动 transcript fallback 由 `agent.settings.capture_mode="transcript"` 显式开启；
  开启后不绑定具体 harness，只要 agent run metadata 中有可读取的 transcript log。

用途：

- Skill evolution。
- Natural-language memory mining。
- Agent-system evolution。
- Reflection、behavior distillation、failure analysis。

限制：

- 不伪造 token ids、logprobs 或 loss mask。
- 不能直接用于 token-level RL。
- 订阅模式只能在 pure-text evolution mode 下使用；订阅不等于 token-level capture。
- 如果 `auth_mode="subscription"` 但没有开启 transcript capture，harness 应拒绝运行，而不是
  静默产生无 completion 的 session。

## 新算法接入流程

优先通过 worker/method registry 接入新方法，不要把方法逻辑硬编码进 gateway、store 或
调度分支。

1. 明确输入：需要 dataset、旧 artifacts、外部训练产物，还是只需要 `job.config`。
2. 明确输出 artifact type：`text_memory`、`skill_bundle`、`agent_system`、
   `parametric_memory` 或新的 typed artifact。
3. 实现 method：接收 `WorkerClaimedJob` 和 `artifact_root`，返回
   `list[ArtifactRegisterRequest]`。
4. 注册 method：内置 baseline 放入 `src/polar_evolution/methods.py` 的
   `METHOD_REGISTRY`；专用 research worker 可以维护自己的 registry。
5. 设置 `compatibility`：限制 task tags、agent harness、base model，避免 artifact 污染
   不兼容 session。
6. 设置 `lineage`：记录输入 dataset、旧 artifacts、training run、adapter source 等。
7. 设置 `scores`：至少给 context resolver 可排序的 `quality` 或
   `heldout_reward_delta`。
8. 添加测试：覆盖 method 输出、worker complete、artifact registration、context resolve 和
   runtime injection 中受影响的路径。
9. 更新文档：见“文档同步要求”。

除非现有 `ArtifactRegisterRequest.manifest` 无法表达新产物，否则不要新增 backend schema。

## Issue / PR 工作流

发现 bug、定位新需求或准备做非平凡改动前，先提 issue。Issue 是需求和缺陷的权威入口。

Issue 应包含：

- 背景和触发场景。
- 当前行为。
- 期望行为。
- 影响范围。
- 可复现命令、日志或样例数据。
- 初步验收标准。

修复或实现必须通过对应 PR 解决 issue：

- PR 标题或描述应引用 issue。
- PR body 中使用 `Fixes #<issue>`、`Closes #<issue>` 或 `Resolves #<issue>`。
- 如果一个 PR 只解决 issue 的一部分，明确写 `Part of #<issue>`，不要误关闭。
- 如果开发中发现 scope 变化，先更新 issue，再继续改代码。

小型文档错别字、纯机械格式化或用户明确要求的快速本地实验可以不先提 issue，但一旦会影响
接口、行为、训练数据、runtime 注入、artifact contract 或开发流程，就应有 issue。

## 文档同步要求

所有改动都应该有对应的文档变化。没有文档变化的 PR 需要在 PR body 中明确解释原因。

必须同步文档的情况：

- Public API、HTTP API、Pydantic model、artifact contract、job/config schema 变化。
- Gateway/runtime/harness 行为变化。
- Capture mode、trajectory builder、evaluator、training signal 变化。
- Evolution method、artifact type、context resolver、adapter merge 行为变化。
- 新增环境变量、runtime 文件、CLI 参数、配置字段。
- 修复了用户可见 bug，并改变了推荐用法或排障方式。
- 新增开发流程、测试流程或 issue/PR 约定。

优先更新位置：

- 架构或接口变化：`docs/architecture/`。
- 模块内行为：对应目录的 `README.md`。
- 仓库级流程和接口约定：本文件。
- 实验/debug 记录：`docs/dev/`，如果该目录不存在可先创建。

文档应写清：

- 改了什么。
- 为什么改。
- 谁消费这个接口。
- 输入/输出 contract。
- 限制和不支持的场景。
- 验证方式或示例命令。

## CI / GitHub 机制建议

当前 issue-first、PR resolves issue 和 docs-required 是仓库协作约定。要把它们变成自动或
半自动机制，建议分层实现：

1. Issue template：在 `.github/ISSUE_TEMPLATE/` 中提供 bug 和 feature 模板，强制填写
   背景、当前行为、期望行为、复现方式和验收标准。
2. PR template：在 `.github/pull_request_template.md` 中加入 checklist：
   - linked issue：`Fixes #...`、`Closes #...`、`Resolves #...` 或 `Part of #...`。
   - docs updated：列出更新的文档路径，或解释为什么不需要文档变化。
   - tests run：列出 focused tests 和结果。
3. CI docs check：脚本检查非纯文档 PR 是否包含 `docs/`、`README.md`、`agents.md` 或模块文档变化；
   如果没有，则要求 PR body 包含明确的 `No docs needed:` 说明。
4. CI issue link check：脚本检查 PR body 是否包含 issue reference。对纯本地实验、机械格式化或
   明确标注的 no-issue 小改可以允许维护者 override。
5. CODEOWNERS/review rule：让 `docs/architecture/`、`src/polar_evolution/`、gateway/trajectory
   contract 相关改动至少经过一名熟悉接口边界的人 review。

这些机制应该先以 PR checklist 和 warning 形式落地，再升级为 blocking CI，避免早期规则过硬
影响研究迭代速度。

## 开发流程

1. 先看 issue：确认当前工作对应哪个 issue；没有 issue 且不是明显小改动时，先创建 issue。
2. 读上下文：用 `rg` / `rg --files` 找入口、测试、相邻实现和相关文档。
3. 明确边界：不要把 research method 逻辑硬编码到 gateway/store；保持调度层只认接口和
   artifact contract。
4. 测试先行：新增行为或 bugfix 先写会失败的 focused test，再实现。
5. 小步修改：优先改最小相关模块，避免顺手重构无关代码。
6. 同步文档：每个行为/API/流程变化都要更新文档，或在 PR 中说明为什么不需要文档变化。
7. 保护用户改动：工作区可能已有未提交改动；不要 revert、checkout 或覆盖非本任务改动。
8. Review diff：提交前必须人工看 `git diff`，重点看 public API、config parsing、
   path handling、artifact selection、registry import order、fallback/error semantics。
9. 验证：至少跑 focused tests 和 `git diff --check`；涉及公共入口、runtime 组合或
   evolution backend 时跑相关回归集合。
10. PR 关联 issue：PR 描述必须说明解决哪个 issue，并写明验证结果和文档变化。

## 常用命令

安装开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

运行全部测试：

```bash
pytest -q
```

运行 focused tests：

```bash
pytest tests/test_evolution_agent_harnesses.py -q
pytest tests/trajectory/test_agent_transcript_builder.py -q
pytest tests/gateway/test_evolution_integration.py -q
pytest tests/evolution/test_datasets_jobs.py -q
```

提交前检查 patch：

```bash
git diff --check
```

Lint：

```bash
ruff check .
```

仓库历史上可能存在未格式化文件。不要为了单个改动运行全仓库格式化并提交大规模无关
format churn；只格式化本次触碰的 Python 文件。

## Review Before Commit

提交前必须完成：

- `git status --short`：确认会提交哪些文件，区分自己的改动和已有用户改动。
- `git diff --check`：确认没有 trailing whitespace 或 patch 格式问题。
- `git diff`：人工 review 行为改动。
- Tests：跑与改动相关的 focused tests，并记录命令和结果。
- Docs：确认有对应文档变化，或在 PR body 中解释为什么没有。
- Issue link：确认 commit/PR 关联对应 issue；PR body 使用 `Fixes`/`Closes`/`Resolves`
  或 `Part of`。
- Import-order：新增 registry/getter/factory 时，用 fresh process 或 subprocess 测试，避免
  import side effect。
- Compatibility：修改 config schema、artifact schema、context selection 或 builder 行为时，
  保留明确要求兼容的旧路径，并补回归测试。
- Artifacts：涉及 evolution、多轮、adapter 或 context resolve 时，确认不会复用 stale
  artifact，不会选择不兼容 artifact。

不要在未 review diff、未验证测试结果时提交。不要把 `.pytest_cache/`、`__pycache__/`、
runtime 输出、dataset dump、adapter 权重或 secret 文件提交进仓库。

## 测试选择建议

- Codex/Claude/Gemini/OpenHands harness 变更：`tests/test_evolution_agent_harnesses.py`。
- Gateway evolution context 或 runtime 注入：`tests/gateway/test_evolution_integration.py`。
- Completion/proxy transform：`tests/gateway/test_transform_*.py` 和相关 gateway tests。
- Trajectory builder：`tests/trajectory/`。
- Evolution store/API/jobs/artifacts/context：`tests/evolution/`。
- Parametric memory adapter selection：优先覆盖 context resolver 和 gateway proxy request
  path。
- Pure-text transcript capture：覆盖 parser、gateway fallback、dataset ingestion 和 downstream
  evolution compatibility。

## Git 约定

- 不使用 destructive git 命令，除非用户明确要求。
- 提交前不要自动 stage 全部文件；先用 `git status --short` 和 `git diff` 确认范围。
- 如果需要 commit，只提交本任务相关文件。
- Commit message 用简短祈使句或 Conventional Commit，例如
  `feat: add transcript evolution capture`。
- Push 前确认当前 branch 和 upstream，避免推错 remote。
- Merge conflict 必须人工审核每一个冲突 hunk，理解两边语义后再解决；不要使用自动
  ours/theirs 策略批量覆盖。
