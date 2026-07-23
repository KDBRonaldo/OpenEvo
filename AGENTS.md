# OpenEvo 开发指南

本文件给 Codex/开发者提供仓库级协作约定。开始改代码前先读本文件，再按相关模块
README 和 `docs/architecture/` 补上下文。

## 项目定位

产品目标、支持范围和 release acceptance 只以
`docs/maintainer/productization/spec.md` 为准。本文件规定实现与协作方式，不是第二份产品
spec。

OpenEvo 是面向真实 agent harness 的 evolution 系统，对外只有两个应用：

- **OpenEvo Daemon**：运行在远程 Linux 服务器上的后端，统一拥有执行、
  trajectory/transcript capture、dataset/job/artifact、method registry、context resolve、
  runtime injection、deployment lifecycle 和 backend API。
- **OpenEvo Desktop Client**：面向普通科研用户的 macOS 桌面应用，用于配置远程服务器、
  安装或连接 OpenEvo Daemon、运行 science task，并监控 memory、skill、agent-system 等
  演化过程。

`src/openevo/` 中的 OpenEvo Core 是 Daemon 使用的共享实现，不是第三个用户产品。

不存在单独发布的 CLI 或开发套件产品面。保留的命令行入口只是后端 launcher、维护工具、
CI 工具或开发者自动化工具，不能在公开文档里包装成普通用户产品。

Runtime/data identity 使用 `OPENEVO_*` 环境变量、`/openevo/session` runtime path、
`.openevo/evolution` state root 和 `openevo.session_completed` event type。不要在新的
公开 contract、文档、测试或示例中引入 legacy runtime markers。

OpenEvo Core 的核心目标是把 agent 运行过程稳定地转成可训练、可评估、可演化的数据，并
把演化后的 memory、skill、agent system 和 parametric memory 注入后续 session。

当前系统有两条明确的数据路径：

- Token-level training path：agent 经过 OpenEvo Core LLM proxy，Core 捕获 completion
  records，并构造包含 token ids、loss mask、logprobs 的 trajectory。
- Pure-text evolution path：agent 不一定经过 Core proxy，Core 或外部 harness 只要求
  提供可解析的 text/transcript trajectory，用于 skill、memory、agent-system 等文本
  自进化。

不要把 subscription auth 和 capture mode 混为一谈。Subscription 只是某些 harness 的
认证/运行方式；pure-text evolution mode 才是订阅模式可用的数据形态。订阅模式必须显式
设置 `agent.settings.capture_mode="transcript"` 或等价 transcript capture mode 才允许生效。
Codex subscription 只是第一个落地 case，Claude Code、Gemini CLI、OpenHands 或其他
harness 只要能提供稳定 transcript，都应能接入 pure-text evolution。

所有 evolution/training 方法统一只在下一 task/session 生效。一个 immutable task 的 closed
admission record 必须 pin 同一 project head、workspace snapshot、evolution revision 和 verified
runtime context/execution snapshot；它的所有 infrastructure attempts 复用该 admission。task 完成后才封存
dataset、在 inference 进程外运行 enabled methods，并把 accepted workspace result 和全部 target
outputs/runtime context 作为一个 successor project head 原子提交。下一 task 在该 head 未完成时
只能保留 draft，提交必须返回 not-ready 且不能创建 Task/admission/run，不能静默使用
stale/partial state。不要在 method descriptor 或 method
config 中加入各自的 online/offline timing、background/barrier 选择或 streaming/in-session ABI。
方法差异继续通过 input bindings、execution/capture/harness/runtime requirements、config 和 typed
outputs 表达。
这是产品目标 contract；Store 当前只提供 atomic successor ledger commit primitive，完整的
cross-session run-owner/readiness orchestration 尚未实现，不能把内部 ledger、materializer 或现有
legacy Gateway path 描述为已经满足该 contract。

Desktop/Daemon 的当前实现边界由
`docs/architecture/desktop-core-contract-v2.md` 在 canonical spec 范围内定义；冻结的 v1
只作为 0.1.8 历史 contract 和只读迁移输入。React renderer 只能调用带 Desktop session
鉴权的 `/desktop/v2/*` 本地 sidecar API，不能看到
SSH command、Core URL、backend token、secret reference、host path 或 benchmark 概念。
Sidecar 只在 Daemon 存在前负责 SSH/bootstrap；Daemon 通过兼容性检查后，run、service、
artifact、Project Head、Evolution Revision 和 diagnostics 必须走 active project tunnel 上的
`/v2/*` control API，不能退回
SSH 直接执行。两个 API 都必须有严格 closed model、version/digest negotiation、typed error、
幂等 action、游标和可恢复事件语义。Contract simulator 只能用于显式 debug/test build；release
遇到 simulator、scaffold、dry-run、legacy route fallback、无 verified registry 或不兼容 digest
时必须 fail closed。

## 目录结构

- `src/openevo/`: OpenEvo Core implementation、Daemon backend、Core-owned
  science/project config、deployment lifecycle、backend API 和 developer automation
  helpers。
- `desktop/`: OpenEvo Desktop Client 的 React/Vite/Tauri 前端、native host、sidecar 和
  打包资源。
- `benchmarks/`: standalone benchmark automation packages。Benchmark-specific code
  must live outside `src/openevo` and `desktop` and import/call Core capabilities。
- `benchmarks/terminal_bench/`: independently installable Terminal Bench maintainer
  automation、Harbor I/O、reporting、frozen gate manifests 和 package-local tests；
  Core/Desktop 不得 import 或 package 它。
- `src/openevo/harness/`: agent harness contract 和 Codex、Claude Code、OpenHands 等 presets。
- `src/openevo/gateway/`: gateway server、LLM proxy、runtime lifecycle、completion capture、
  evolution context 注入。
- `src/openevo/trajectory/`: completion/session/transcript 到 `Trajectory` 的 builder 和
  evaluator。
- `src/openevo/runtime/`: Docker/Apptainer runtime 抽象。
- `src/openevo/rollout/`: rollout server、pipeline、balancer、result aggregation。
- `src/openevo/config/`: topology/config model。
- `src/openevo/platform/`: observability platform APIs and helpers。
- `src/openevo/evolution/`: Evolution Backend，包含 events、datasets、jobs、workers、
  artifacts 和 context resolver。
- `src/openevo/evolution/artifact_payloads.py`: Core-owned `file://` payload scanner、
  ephemeral opaque handle 和 bounded verified text-read service；只允许 Core-managed
  artifact root 内 link-count-one regular files/directories。Scanner 从绝对 filesystem anchor
  逐组件 no-follow 固定并记录 allowed-root identity，持有稳定 root FD，并在每次操作前后复核
  pathname 与 held FD binding；payload traversal 只相对该 FD 进行。它对一个 service 生命周期内
  所有 scan 和 verified-reread attempts 施加不可退回的累计 node/file/byte budget；失败
  snapshot/read 已经消耗的枚举与 hashing 资源同样计入。
- `src/openevo/evolution/context_projection.py`: 内部 projection v1 resolver；只通过
  verified executable registry 调用 target handlers，并持久化 validated data-only
  contributions。它使用有元素与总 byte 上限的 strict closed agent/metadata contract，
  不持久化任意 env/secret 字段，也不是 legacy `/v1/contexts/resolve` 的公开兼容 response。
- `src/openevo/evolution/context_materialization.py`: Core 内部 generic materializer；绑定同一
  verified executable registry，把 validated contributions 流式复制为 private opaque-ID、
  digest-verified blobs，解析 runtime env/instruction/adapter spec，并通过临时目录、fsync 和
  rename 原子发布。
  它不得按 target ID 分支，也不得在持久化 contract 中暴露 artifact URI、host path 或 opaque
  scanner handle。
- `src/openevo/evolution/revisions.py`: immutable cross-session project-head ledger 与 task
  admission contract。这里内部名为 revision 的 ledger row 对应产品 spec 的 Project Head
  composition；不要把它与仅表示 artifact set 的产品层 Evolution Revision 混为一谈。
  Store 允许显式 generation-zero head，并提供严格相邻 successor 的 atomic ledger commit primitive；
  当前 generation admission 固定 exact revision，下一 generation 持久化为 queued/not-ready。Activation
  在同一事务要求 queued request identity 匹配候选 revision，且仍未 pin 的当前代 queued admission 会阻止
  后续 head 推进。该 primitive 不提供 transition/readiness 证明或 run-owner 编排，不得增加绕过这些阶段
  的任意 revision publish 入口。这些 queued rows 是未接入产品的 Store 内部 primitive，不是 Desktop
  可提交的 queued Task；普通用户路径必须遵守 canonical spec 的 draft/not-ready 语义。Execution snapshot 使用 closed typed
  model/runtime/serving data；model source 必须显式声明 `hugging_face`、`managed_snapshot` 或
  `subscription`，不能持久化 host path/URI。Store 只接受 verified producer sealed 的
  `VerifiedExecutionSnapshot`，再从 typed canonical bytes 计算 ID/digest 并记录 producer ID；该对象不能
  由普通调用方构造。当前没有 production issuer，只有仓库私有 testkit 可签发测试对象；#160/B1 managed
  deployment 接入 verified producer 前，release genesis/admission 必须 fail closed。Revision 必须引用且
  内嵌 exact registered snapshot；该内部 primitive 不证明 B2 deployment、serving readiness 或
  attestation。Task admission envelope 只能包含 allowlisted non-secret identity
  fields 和 content-addressed project/workspace/task refs；schema 不接收 raw instruction、credential、env、
  setup command 或开放 runtime/model dict；非闭集字段在输入边界直接拒绝。未来 Core run owner 应从 immutable
  project/workspace snapshots 构造该 envelope；当前尚未接线。Subscription 只能使用 transcript capture。
- `src/openevo/experiments/`: experiment compiler/runner/promotion helpers。
- `src/openevo/projects/science/`: ordinary-user science project config/compiler。
- `src/openevo/deployment/`: remote deployment lifecycle、bootstrap、preflight、SSH transport。
- `docs/architecture/`: OpenEvo Daemon/Core/Desktop 架构、evolution API、runtime
  context、worker 接口文档。
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
session/task events -> dataset artifact -> immutable evolution plan
  -> plan-bound job -> verified worker method
  -> typed artifacts -> context resolve -> gateway runtime injection
```

Cross-session project-head ledger 使用 canonical manifest digest 绑定 project/workspace snapshots、
materialized context/artifact set、registered effective execution snapshot 和 ordered adapters。当前 generation 的
admission 必须在同一 `BEGIN IMMEDIATE` 内 exact 匹配 stream/project、execution/capture mode、snapshot、
context 和 artifact set；queued row 在 activation 后重启时允许保持未 pin，exact retry 再原子 pin active
revision。未 pin `cancelled` 是保持完整 request sources 与 no-pin 语义的 closed historical audit row，head
继续推进后不再要求其 generation 与当前 active generation 相邻；pinned `cancelled` 仍必须验证原 pin 的完整
闭包，但不要求该 pin 仍是 active head。Task/idempotency identity 不可复用到不同请求；admitted pin 和
terminal state 不可改写。
Startup 必须按 exact schema、canonical rows、predecessor chain、stream head 和 pinned identity fail closed
校验。Store identity、context snapshot/materialization 和 B3 ledger 的 text/blob recovery 必须先只读取
PK 与 SQL `length(CAST(value AS BLOB))`，在不可退回的 row/aggregate budget 允许后，才以 exact-length
guarded `CASE` 逐行取值并重验 UTF-8 bytes；不得让超限单值先进入 Python。该保证不泛化到尚未迁移的
legacy job/artifact recovery。Task admission 首次写入与 recovery 使用同一 row/aggregate byte capacity；每次
非幂等状态 UPDATE 必须先按 old/new exact UTF-8 byte delta 验证容量，并在 UPDATE 后 readback 重验，exact
retry 不因已满而失败。`get_active_revision` 与 `get_task_admission` 必须在显式一致 read transaction 内分别
执行 stream/admission 的完整权威闭包校验。Genesis、successor activation、queued、admitted 与 terminal
retry 以及 terminal transition 都必须在同一事务走 active head、revision、materialization、execution
snapshot 和完整 envelope/pin 的统一权威闭包校验。当前内部 ledger commit primitive 不等于完整
successor readiness/activation orchestration，也尚未接入公开 Core API、Gateway、run owner 或 Desktop。

新的 Core experiment job 必须通过 `POST /v1/planned-jobs` 创建。Plan-bound job 持久化
plan、plan digest、target、reachable registry digest、method identity、canonical execution envelope 和有序
input artifact snapshots，并独立保存完整 envelope digest；复用 plan ID 时必须同时匹配
schema version、registry snapshot digest、plan digest 和 canonical plan JSON；同一 plan/target 的相同请求幂等返回
同一 job，不同请求拒绝。Worker
claim 必须提交 verified method ID -> identity digest mapping，store 只租出 exact match，并在
发 lease 前对照当前 frozen registry 校验 persisted plan/envelope/input snapshots；损坏的 selected row 会先隔离为 failed。
Complete 必须再次完成同一校验并符合 active descriptor 声明的 output types。Outputs 由 job
拥有并先 staged，不得在 job success 的最终事务前被读取、promotion 或 resolve；失败、lease
过期和启动恢复会清理不可恢复的 staged rows/files。Claim 必须持久化请求的 lease duration，
heartbeat 按该 duration 续租；所有 lease clear 路径同时清空 duration。Startup 在 DB recovery
transaction 内将 Core-owned managed artifact manifests 与 DB rows reconciliation，提交后幂等删除
无引用 orphan，且不得跟随 artifact root 外部 symlink。`POST /v1/jobs`/`METHOD_REGISTRY` 只暂留给尚未迁移的 benchmark automation，
不能作为 plan 校验或 verified dispatch 失败后的 fallback。

Project/experiment evolution config 只使用
`evolution.targets.<target_id> = {enabled, method, config}`。启用的 target 必须显式选择
method；禁用 target 可以保留 draft method/config。不要重新引入旧的 Science flat
booleans、experiment 顶层 `artifacts` config 或兼容 wrapper。`agent_system.method=auto`
只由 Core 根据当前 round 开始前的 prior dataset IDs 解析，job/lineage 必须记录 concrete
method、requested value 和 resolver 输入。为了让 Desktop 无损保存未知 method config，project
config integer 只允许 JavaScript safe-integer 范围；integral float 规范成 integer，超出范围的
integer 应使用 string 表达。

Evolution capabilities 只能由远程 Core 的同一 `VerifiedExecutableRegistry` 生成。
该 executable registry 必须携带 frozen snapshot 中全部且仅有的 distribution
attestation；每个 target、handler 和 method identity 的 distribution/version/digest 都必须
与对应的已验证安装一致。非执行 target anchor、可执行 handler handle 和 method handle
集合必须分别与 snapshot 精确相等；handler 不能再退回 identity anchor。
`VerifiedDistribution` 和 `VerifiedExecutableRegistry` 只能由完成 wheel/install/inventory 与
逐 entry-point 校验的 loader sealed publication path 创建，不能提供公开构造或 loader 注入
绕过。公开 distribution verifier 必须只使用真实 installed-distribution discovery；测试 metadata
provider 只能通过仓库 testkit 的私有路径注入。
`GET /capabilities?execution_mode=<release-mode>` 返回 target-rooted
`EvolutionCapabilitiesV1`，包含 registry digest、evaluated profile、configured/effective
default 和四个 support axes。Desktop sidecar 的对应 endpoint 必须要求本地 mutation token
并通过 active Core tunnel 转发；禁止重新引入本地 method table、`/desktop/methods` alias 或
Desktop-bundled Core fallback。缺 registry、tunnel 或有效 remote payload 时必须 fail closed。
Target 的 `methods` 是当前 audience 可展示的新选择；`accepted_methods` 是 Core registry
仍接受、但不一定应在普通用户 UI 展示的已有显式选择；`selection_resolvers` 描述 Core-owned
项目选择值（当前为 `agent_system.method=auto`）及其可能解析到的 concrete methods。不要把
三者混成一个 method table；resolver concrete method 的 identity/support 必须与
`accepted_methods` 同 ID 条目完全一致。
Method descriptor 的 `project_config_injections` 以 field/source 对声明由 Core compiler
确定性注入、普通用户不应编辑的顶层 config 字段。source 只能使用 framework 已实现的闭集；
字段必须存在于完整 method schema。Desktop capability projection 从 schema/default/required
中移除它们，experiment compiler 必须先删除 project/stale 值，再从权威 source 注入并执行
verified registry 完整规范化。未声明 injection 的同名字段保持 user-owned，不得按 target ID
或字段名猜测覆盖。注入字段不得嵌入顶层 object schema 的 `default`、`const` 或 `enum`
annotation，避免投影后丢失字段关联语义。
Desktop 对空 method 或远端已删除/不支持的 method 重新启用时，必须绑定远端 effective
default 并使用其 default config；仍受支持的显式 method/config 必须原样保留。任何已启用但
无效的 target/method 都必须可见、可关闭，并阻止 run，capability 获取失败必须提供同 mode
重试而不能使用本地静态表。未保存 draft 不能放行已激活 session；Sidecar 的 run endpoint
必须在每次远程启动前重新读取 capabilities，并按 active project/mode fail closed 校验。
Sidecar 随后必须携带 capability registry digest 调用远程 Core
`POST /evolution/project-validation`；Core 对 visible、hidden accepted 和 resolver 的全部可能
concrete methods 使用完整 registry/schema 和 compiler injection 做同步校验，成功前不得创建
run thread 或执行远程 run command。
该 endpoint 的 UTF-8 request body 上限为 1 MiB，必须在 JSON 解析前按实际接收字节和结构
深度 fail closed；target map 最多 128 项，method config 同时受深度、节点、集合项和文本总量
预算约束。Desktop sidecar 必须使用同一字节上限并在发起远程请求前校验序列化后的 payload。
存在 active project session 时，capability 和 run 校验只能使用该 session 的 SSH tunnel，不能
退回 launcher 的共享 backend URL。

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

- Gateway 写入 `/openevo/session/evolution/memory.md`。
- 设置 `OPENEVO_MEMORY_FILE`。
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

- Gateway stage 到 `/openevo/session/evolution/skills/`。
- Copy-based harness 通过 `OPENEVO_SKILLS_DIR` 加载。
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

- Gateway 写入 canonical `/openevo/session/evolution/agent_system.md`。
- Gateway 尝试写入 runtime workdir 下的 `target_path`。
- 设置 `OPENEVO_AGENT_SYSTEM_FILE`、`OPENEVO_AGENT_SYSTEM_TARGET` 和
  `OPENEVO_AGENT_SYSTEM_TARGETS`。
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

- Agent 通过 OpenEvo LLM proxy 调 OpenAI/Anthropic/Google compatible APIs。

产物：

- `CompletionSession`。
- Token-level `Trajectory`。
- `Trace.response_ids`、`Trace.loss_mask`、`Trace.response_logprobs` 可用。

用途：

- Token-level RL。
- Policy gradient。
- 需要 sampled-token logprob 或 loss mask 的训练。

限制：

- 必须走 OpenEvo proxy。
- 如果 harness 使用外部订阅直连模型服务，则不能声称有 token-level metric。

### Pure-text evolution mode

入口：

- Harness stdout/stderr transcript。
- Agent run transcript。
- 外部 text trajectory。
- 不要求经过 OpenEvo proxy。

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

优先通过 evolution framework registry 接入新方法，不要把方法逻辑硬编码进 gateway、
store 或调度分支。`src/openevo/evolution/framework/builtins.py` 是当前内置 target、handler
和 method descriptor 的权威目录；`builtin_handlers.py` 只实现 descriptor 指向的可信 runtime
projection callable。Release startup 从 Desktop/维护者提供的外部
`framework-lock.json` 校验 exact wheel 和安装 inventory，再发布
`VerifiedExecutableRegistry`；不能自动发现、自动启用插件，也不能从运行中的代码自算 digest
后信任。

每个 method descriptor 必须声明一个进入 canonical identity 的 invocation ABI：已有方法使用
`legacy_worker_job_v1` 的 `(job, artifact_root)`；新 contract method 使用
`method_context_v1` 的 `(context)`。Loader 校验精确参数名和 kind，worker 按 ABI 分发，不猜
signature。当前 release composition 只加载 built-ins；外部 research plugin 和任意新 target
仍需完成 registry composition、generic projection 和 release tests，不能只凭 descriptor 测试
声称端到端可运行。

Target handler 只能返回 data-only contributions。普通环境绑定必须引用 staged payload；需要
暴露 `harness_skills` 等 Core-resolved 公共根目录时使用 `scope_root` binding，并明确声明一个
descriptor allowlisted destination scope。Handler 不得返回 host path、命令或自行解析 runtime
root；payload scanner/materializer 才负责 no-follow 读取、digest 重验和原子 staging。
Materializer 必须重新签发 staged/adapter payload inventory；每个 staged file 在复制时验证
identity、size 和 digest，adapter inventory 必须完整重新 hash。Bundle 先作为 private 临时目录
写入并 fsync，再原子发布；publication receipt 必须绑定 canonical manifest bytes、bundle/blob
directory identity 和逐 blob identity，DB commit 前在同一个 locked root FD 下重新复核。DB
持久化失败与启动恢复只能对已绑定 inode 做保守 discard/reconciliation：随机 quarantine 后清空
可安全固定的内容并保留 quarantine/tombstone entry，不得声称 pathname 已立即删除。rename 后的
最终路径校验失败不得按名称删除可能已被替换的目录，应留给启动恢复。数据库 store ID、
resolved artifact root、artifact root marker 和 materialization-root marker 必须全部匹配；
identity DDL 与 pending row 必须在同一 SQLite transaction
创建，首次/legacy binding 使用可恢复的 pending -> 两个 marker fsync -> bound 协议，bound 状态
缺少任一 marker 必须 fail closed。发布/DB commit/恢复清理共享跨进程锁。Blob transport/consumer
只能通过 `EvolutionStore` 拥有的入口；该入口持有并复核同一个 locked materialization-root fd、
store identity 双 marker、owner/mode/inode binding，从 SQLite
`context_materializations.manifest_json` 读取权威 `MaterializedContext`，再把 expected manifest
和 root fd 传给低层 materializer。磁盘 `manifest.json` 必须逐字节匹配 DB 中的 canonical
manifest，不能通过同时修改 blob 和磁盘 manifest 自证。低层 reader 必须 no-follow 打开 blob，
重验 size/digest/path identity，并且只暴露受控 read-only stream，不能暴露 raw fd 或 host path。
公开 strict v2/Gateway 尚未切换前，内部 materializer 不能作为 legacy
`/v1/contexts/resolve` 的 fallback 或 shadow response。
Materialization root 必须 owner-verified 且 mode `0700`；publish、precommit、discard 和 recovery
必须复用同一个 locked root fd。清理必须绑定首次观察到的 inode；随机 quarantine/tombstone 是
保守处置而不是删除完成。Identity mismatch 应保留，且后续 recovery 持续 fail closed 直到明确
处理。除 fresh/recognized pending bootstrap 外，Startup 只接受精确 allowlist 中的
historical/current schema fingerprint，并独立精确校验 `store_identity` schema/row 和双 marker；
只有 complete fingerprint 可认领已有 managed recovery state。伪造或 near-match identity 必须在
任何 recovery cleanup 前失败且不清理。Fresh DB 不得认领已有 Core-managed recovery state；
legacy DB 只有在任何 identity DDL/row 或 marker 写入前通过上述识别才可迁移，不能只按表名识别。
最终 bundle 发布必须使用 FD-relative atomic no-replace rename，不能覆盖竞态同名目录；平台不支持
该原语时 fail closed。临时目录初始化失败也只能按已打开 inode 保守 quarantine，不能 pathname
删除。Base schema DDL、additive migrations 和 recovery DB changes 必须在同一个显式 SQLite
transaction 内完成；历史 fingerprint 包含可直接升级的真实 first-parent 布局。Fresh DB 的 managed-state 检查必须同时覆盖 context
snapshots、materializations 和 artifact manifests。DB commit 后或提交状态无法证明时不得 discard
publication，只能保守保留并由后续 recovery 校验。Precommit callback 返回后和 locked root 正常
退出时都必须再次复核 materialization-root binding。
Context snapshot reconciliation 只在 startup 以 DB canonical bytes 授权；普通 read/inventory 始终
要求 link-count-one `0600` regular
file，较宽但安全的历史 mode 只能经显式 startup migration 收紧到 `0600`。Adapter 完整 inventory
rehash 后必须重新绑定 payload-root pathname。
当前 descriptor 必须分别声明 handler input v1、renderer v1 和 output/contribution v2，Core
在 invocation 时逐项校验；output v2 的 adapter contribution 必须绑定 source payload digest
和 byte size，不能把 v1 output 当作 v2 推断。
需要在 runtime instruction 前保留固定 framing 的 target，应在 handler descriptor 的
`instruction_preamble` 声明；materializer 按 projection 顺序使用该数据，禁止 Gateway 按
target ID 拼接文案。Projection response 必须绑定 canonical request digest；materializer 必须复核。
Instruction view 对每个 projection 的合并正文执行 legacy-compatible 首尾 whitespace trim，但
staged artifact/blob 必须保留算法产出的原始 bytes。
只有 source artifact IDs 和正文都完全相等的 instruction/file 投影才能去重；派生 target
文件受两倍投影预算约束，所有文本投影总量受三倍预算约束。Text source MIME 必须满足
descriptor allowlist；skill bundle 必须含根目录 `SKILL.md` 并保持 ranked/canonical renderer
顺序。Adapter manifest 缺失或空的 identity 字段必须由 handler 和 validator 独立按同一
canonical fallback 计算，不能信任 handler 自报值。

1. 明确输入：需要 dataset、旧 artifacts、外部训练产物，还是只需要 `job.config`。
2. 明确输出 artifact type：`text_memory`、`skill_bundle`、`agent_system`、
   `parametric_memory` 或新的 typed artifact。
3. 实现 method：新方法接收 framework `MethodExecutionContext`；当前已有方法继续通过
   legacy adapter 接收 `WorkerClaimedJob` 和 `artifact_root`，返回
   `list[ArtifactRegisterRequest]`。不要修改已有算法函数来适配框架。
4. 注册 descriptor：声明稳定 method ID、target、ordered input bindings、output artifact
   types、closed config schema、execution/capture/harness/runtime support、exposure、maturity
   和 locked implementation entry point。由 compiler 提供而不应展示给普通用户的顶层字段
   还要声明 `project_config_injections` 的 field/source，并确保 compiler 已实现该权威 source。
   不要从旧
   `METHOD_METADATA` 反向生成 descriptor。
5. A2 迁移期兼容：已有内置 legacy 方法仍临时同步到 `METHOD_REGISTRY` 和
   `METHOD_METADATA`，并由 anti-drift tests 证明 descriptor entry point 与 callable 是同一
   对象；plan-bound product jobs 不读取这两张表。新 context method 不得加入 legacy dispatch
   作为 fallback。A2.5 在 benchmark automation 迁移后删除重复表。
6. 设置 `compatibility`：限制 task tags、agent harness、base model，避免 artifact 污染
   不兼容 session。
7. 设置 `lineage`：记录输入 dataset、旧 artifacts、training run、adapter source 等。
8. 设置 `scores`：至少给 context resolver 可排序的 `quality` 或
   `heldout_reward_delta`。
9. 添加测试：覆盖 descriptor graph/identity、verified loading、method 输出、worker complete、
   artifact registration、context resolve 和 runtime injection 中受影响的路径。
10. 更新文档：见“文档同步要求”。

除非现有 `ArtifactRegisterRequest.manifest` 无法表达新产物，否则不要新增 backend schema。

## Issue / PR 工作流

发现 bug、定位新需求或准备做非平凡改动前，先提 issue。Issue 是需求和缺陷的权威入口。

Issue 应包含：

- Issue label：必须明确标注以下 9 种 GitHub issue label 中的一个或多个；如果涉及多类，
  写主 label 并说明次要 label：
  - `bug`：Something isn't working。
  - `documentation`：Improvements or additions to documentation。
  - `duplicate`：This issue or pull request already exists。
  - `enhancement`：New feature or request。
  - `good first issue`：Good for newcomers。
  - `help wanted`：Extra attention is needed。
  - `invalid`：This doesn't seem right。
  - `question`：Further information is requested。
  - `wontfix`：This will not be worked on。
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

1. Issue template：在 `.github/ISSUE_TEMPLATE/` 中提供模板，强制填写 issue label、
   背景、当前行为、期望行为、复现方式和验收标准；label 值只能来自上面列出的 9 种。
2. PR template：在 `.github/pull_request_template.md` 中加入 checklist：
   - linked issue：`Fixes #...`、`Closes #...`、`Resolves #...` 或 `Part of #...`。
   - docs updated：列出更新的文档路径，或解释为什么不需要文档变化。
   - tests run：列出 focused tests 和结果。
3. CI docs check：脚本检查非纯文档 PR 是否包含 `docs/`、`README.md`、`AGENTS.md` 或模块文档变化；
   如果没有，则要求 PR body 包含明确的 `No docs needed:` 说明。
4. CI issue link check：脚本检查 PR body 是否包含 issue reference。对纯本地实验、机械格式化或
   明确标注的 no-issue 小改可以允许维护者 override。
5. CODEOWNERS/review rule：让 `docs/architecture/`、`src/openevo/evolution/`、
   gateway/trajectory contract 相关改动至少经过一名熟悉接口边界的人 review。

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
