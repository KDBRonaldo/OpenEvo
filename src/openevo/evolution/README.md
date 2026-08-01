# OpenEvo Evolution Backend（演化后端）

OpenEvo Evolution Backend 是一个面向 skill、memory、agent system 文本和 parametric
memory evolution 的异步控制面。它接收 OpenEvo session 和 task events，从 events
构建 datasets，把 jobs 租约给外部 workers，注册 workers 产出的 artifacts，并为
后续 OpenEvo sessions 解析 runtime context。

架构文档和图：

- [Evolution Backend](../../docs/architecture/evolution-backend.md)
- [Evolution Runtime Context](../../docs/architecture/evolution-runtime-context.md)
- [Evolution API 与新算法接入](../../docs/architecture/evolution-api-and-method-integration.md)
- [Reference Evolution Worker](../../docs/architecture/reference-evolution-worker.md)

维护者从已安装 release wheel 启动 backend 时必须提供与 wheel 同目录的外部 lock：

<!-- openevo:maintainer-only-command -->
```sh {.openevo-maintainer-only}
python -m openevo.evolution.cli serve \
  --host 127.0.0.1 --port 8200 \
  --framework-lock /path/to/framework-lock.json
```

默认情况下，backend 状态保存在 `.openevo/evolution/` 下。

初始化已有本地 state 时，store 保留 event source 和 event type 原值，不再迁移
pre-release runtime identity。新 producers 和新文档必须使用 OpenEvo identity，
例如 `source="openevo"` 和 `event_type="openevo.session_completed"`。

核心 APIs：

- `/v1/events`
- `/v1/datasets`
- `/v1/planned-jobs`
- `/v1/jobs`
- `/v1/jobs/claim`
- `/v1/jobs/{job_id}/heartbeat`
- `/v1/jobs/{job_id}/complete`
- `/v1/jobs/{job_id}/fail`
- `/v1/contexts/resolve`

Core's managed science run owner, and no public client, consumes the private
authenticated `GET /v1/internal/jobs/{job_id}` observation. The store reads the
job row and all active output artifact rows in one explicit SQLite read
transaction, so concurrent job completion cannot combine an older state with
newer outputs. Non-succeeded snapshots always return an empty `artifact_ids`,
never scan payloads, omit `outputs`, and reduce worker error text to the bounded
`evolution_job_failed` code.

For a succeeded snapshot, outputs are ordered by `(created_at, artifact_id)` and
limited to 128 rows and 4 MiB of serialized result data. A single
request-scoped `ArtifactPayloadService` scans every output `file://` payload
under the managed artifact root, so its no-follow node/file/byte attempt budgets
are cumulative and cannot be reset between outputs. Each closed output contains
only `artifact_id`, `type`, `name`, `manifest`, `lineage`, `compatibility`,
`scores`, `promoted`, `created_at`, and the scanner-issued
`payload_manifest_digest`, `payload_byte_size`, and `payload_file_count`.
Artifact URI, host path, opaque payload handle, raw worker error, and arbitrary
extra fields are never projected. Missing, non-file, outside-root, symlinked,
hardlinked, drifting, or over-budget payloads fail the whole private result
closed.

`artifact_payloads.py` 是 handler runtime 的 Core-owned 本地 payload 安全边界。它只扫描
Evolution Backend 配置的 artifact root 内 normalized `file://` regular
files/directories。构造时从绝对 filesystem anchor 逐组件 no-follow 固定并记录 allowed root，
持有稳定 root FD；每次操作前后复核 held FD/path binding，payload 只相对该 FD 使用
no-follow/nonblocking opens。它生成 ephemeral
opaque handles 和 canonical digest inventory。Linux Core 先用 `O_PATH` 固定并验型节点，
再从 fixed fd 获取可读对象；所有目录/文件节点在排序前受总量限制，snapshot 签发前会重验
每个后代 identity。Text handler 读取时会重新打开并验证完整文件的 identity、digest 和
UTF-8，只返回 character/byte 双重上限内的 prefix，并在读取量超过 snapshot size 时立即
失败。Regular file 必须只有一个 hardlink，避免 managed root 内的 link 仍指向 root 外可见
inode；同一个 request-scoped service 的 snapshots 共享累计 node/file/byte budget。没有
Linux `O_PATH`/`/proc/self/fd` 等价语义的平台会 fail closed。
该累计预算按 scan/verified-reread attempt 即时消费，而不是只在 snapshot 成功签发后记账：
失败候选已经枚举的 node/file 和所有已经读取、hash 的 bytes 不会退回预算。Internal
projection 在 compatibility filter/ranking 前对 DB routing metadata 施加 JSON
depth/node/collection/byte limits，snapshot I/O 前再绑定 canonical manifest/scores；legacy
artifact registration/worker completion 的接受范围不因该内部 contract 改变，超限候选只以
`metadata_policy_rejected` 隔离。

Issued inventory 不是 source bytes 的 immutable copy 或 lease；source 在签发后仍可能变化。
因此任何 byte-consuming read/staging 都必须重新校验 exact size、identity 和 digest。当前
text read、verified stream copy 和完整 payload content verification 都执行该规则，不能只信任
inventory metadata。

`context_projection.py` 已把 scanner、verified executable registry 和 target handlers 接成
内部 projection v1。它保持现有 compatibility filter 和全局 ranking；每个 target 只接收原顺序
的连续 snapshot ranks，handler output 经过单 target 及 context-wide validation，再以
`registry_digest + destination_roots + projections + selection` 持久化。Response 不包含 artifact
URI、host source path 或 opaque handle。无法安全 snapshot 的单个 artifact 会被排除；handler
或 registry contract 失败不会回退 legacy resolver。Subscription profile 在调用 adapter-only
handler 前通用抑制该 target。Skip 只暴露 bounded reason code，不回显 URI/path/error。Artifact
manifest 语义来自注册事务写入 DB 的 deterministic immutable `manifest_json`，projection 不读取可变的 legacy
manifest file；升级前缺少该绑定的 artifact 会以 `unbound_legacy_metadata` 隔离，不能从文件
静默回填，重新注册后才能被新 projection 消费。Payload 的
symlink、hardlink 或 root escape 会被拒绝。Adapter contribution 记录 resolve 时批准的 payload digest 和 byte size，供
materializer 重新扫描后比对。Store 在加载 promoted rows 前施加总 candidate 上限，每个 target
在 payload I/O 前施加 attempt 上限。显式 artifact allowlist 会下推到 SQL；implicit selection
的总上限优先分配给具备 local `file://` 与 immutable manifest binding 的候选，bounded
remote/unbound/metadata-policy skip 不能挤掉可投影候选；skip query 只返回 bounded
compatibility routing data 与 identity/reason markers，不把被拒绝的 source URI、name、
manifest 或 scores 搬入 Python；只有先通过 compatibility filter 的 artifact 才会持久化
typed skip，无法验证 compatibility 的行不会进入 context。Projection request 使用 strict closed agent/metadata schema，
不接受 agent env 或任意 secret-shaped metadata；task tag、artifact ID 元素和完整 canonical
request bytes 都有显式上限。

安全扫描成功不等于 artifact 语义有效。缺少 `SKILL.md`、非法 target path、handler output
contract 违规或 context-wide conflict 都使本次内部 projection 整体 fail closed；Core 不尝试
通过逐 artifact 猜测来掩盖 handler/registry 缺陷。只有 snapshot/transport policy 无法接受的
单 artifact 才进入 typed skip。

`context_materialization.py` 已实现 Core 内部 generic materializer。它要求与 projection 相同的
sealed registry digest 和 canonical request digest，按 descriptor 的 `instruction_preamble` 和
projection 顺序生成 runtime instruction。Instruction view 按 legacy Gateway 语义 trim 每个
projection 合并正文的首尾 whitespace，但 staged bytes 保持原样。Materializer 通用解析
destination scope/environment binding，将 file/directory contribution 展开为 private random-ID、
digest-verified blobs，并完整重验 adapter payload。结果只包含 opaque blob ID、runtime-relative
destination、digest/size/MIME、env、instruction、renderer/provenance 和 adapter spec，不包含
artifact URI、host path 或 scanner handle。Bundle 先写入 private temp tree，逐文件 fsync 后
rename 发布；context 与 materialization manifest 在同一 DB transaction 绑定，DB 失败会 discard
已验证 bundle。Ephemeral publication receipt 绑定 canonical manifest bytes、bundle/blob directory
identity 和逐 blob identity，Store 在 SQLite commit 前用同一个 locked root FD 复核。Rename 后
重新绑定校验失败时不按名称删除可能已被替换的目录，而由下次 startup recovery 保守处理无引用
entry。最终 rename 是 FD-relative atomic no-replace；竞态同名 entry 不被覆盖，原语不可用时
fail closed。只有能证明 DB 没有提交对应 context/materialization rows 时才 discard publication；
已提交或状态不明时保守保留。临时目录初始化失败只按已打开 inode quarantine；Store 在 callback
后及 locked root 正常退出时复核 binding。发布/commit/startup recovery 共享跨进程锁。DB store ID、resolved
artifact root 与两个 fsynced identity marker 必须匹配后，启动恢复才会保守 reconcile 没有 DB
引用的 bundle/temp/symlink entry。

Blob transport/consumer 的唯一入口由 `EvolutionStore` 拥有。该入口持有并复核与上述操作相同的
locked materialization-root fd、SQLite store identity、artifact-root/materialization-root 双 marker
以及 root 的 owner/mode/inode binding；它从 `context_materializations.manifest_json` 读取并校验
权威 `MaterializedContext`，再把该 expected manifest 与同一个 root fd 传给低层 materializer。
Bundle 内 `manifest.json` 的 bytes 必须与 DB 权威 canonical manifest 逐字节相等；修改 blob 后再
同步修改磁盘 manifest 不能建立新的可信 binding。低层 reader 相对该 root fd no-follow 打开
blob，重验 private path identity、exact size 和 digest，并且只向 consumer 暴露受控 read-only
stream，不暴露 raw fd 或 host path。
Materialization root 必须由 Core 进程用户拥有且 mode 为 `0700`。Store 将同一个 locked root fd
传给 publish、DB precommit、discard 和 recovery；清理候选绑定枚举时 inode，随机 quarantine 后
只清空可安全固定的内容并保留 quarantine/tombstone entry，不能把该处置称为立即删除。
Identity mismatch 会改名保留，之后的 recovery 持续 fail closed，直到维护者明确处理。Adapter 完整
rehash 后重新绑定 payload-root pathname。

`EvolutionStore.initialize()` 在 `<artifact-root>/.openevo-store.json` 和
`<artifact-root>/context_materializations/.openevo-store.json` 写入并 fsync 相同的 closed
`{contract_version, store_id}`；SQLite `store_identity` 同时持久化该 store ID、resolved
artifact-root path 和 `pending -> bound` bootstrap 状态。首次新建或从旧数据库迁移时，identity
DDL 与 pending row 在同一 SQLite transaction 内创建，再 fsync 两个 marker，最后转为 bound；
只有 pending 可恢复中断的 marker 写入。Bound 状态缺少任一 marker、marker 是 symlink、两个 ID
不一致或 DB/path mismatch，都会在 recovery 枚举或处置前 fail closed。这两个 marker 是
Core-owned runtime state，不是 artifact。
Fresh DB 若发现已有 context materialization 或 managed artifact manifest 会拒绝认领。除
fresh/recognized pending bootstrap 外，Startup 只接受 exact allowlist 中的 historical/current
schema fingerprint；`store_identity` schema、row 和双 marker 另行精确校验，且只有 complete
fingerprint 可认领已有 managed recovery state。伪造或 near-match `store_identity` 必须在任何
cleanup 前失败并保留 managed state。Legacy 数据库只有在无 identity table 且 fingerprint
校验发生于 identity DDL/row 或任一 marker 写入之前时，才能迁移已有 Core-managed 状态；仅匹配
表名不足以获得迁移资格。普通用户预先放在 artifact root 非保留目录中的任务文件不受该检查影响。
Fresh 检查也包含 `contexts/` 下的任何 snapshot/tombstone/unknown entry。Base schema DDL 在显式
SQLite transaction 中与 additive migrations、recovery DB changes 一起原子提交；allowlist 包含可由
当前 Store 直接升级的真实 first-parent 布局。

Context snapshot recovery 由 startup 从 SQLite 读取 canonical request/response bytes 后授权，逐项
核对 DB-referenced snapshot，并把无引用 snapshot 保守转为 tombstone。普通 read/inventory 只接受
link-count-one、mode `0600` 的 regular file。历史 owner-readable/writable、non-executable 且非
group/other-writable mode 只在显式 startup migration 中接受，用于原 inode 收紧到 `0600`；普通
读取不提供 legacy mode fallback。

该内部 projection/materializer 尚未替换公开 legacy `/v1/contexts/resolve` 和 Gateway staging；
公开 runtime 路径保持原行为，直到严格 v2 client、opaque blob transport 和 Gateway generic
staging 可以原子切换。
Store 已实现 cross-session revision/admission 的内部持久化 primitive：immutable generation-zero 与
严格相邻 successor manifest 绑定 content-addressed project/workspace refs、canonical materialized
context 及 artifact set、
registered execution snapshot 和 ordered adapters。Execution snapshot 是 closed typed
model/runtime/serving data；Store 只接受 verified producer sealed、普通调用方不可构造的
`VerifiedExecutionSnapshot`，再从 typed canonical bytes 计算 ID/digest 并记录 producer ID。Revision 同时引用并内嵌 exact
snapshot，genesis transaction 从 DB 重读并完整比对。Model identity 拒绝 host path/URI；subscription
snapshot 只能使用 transcript capture、subscription model/client/serving 且不能带 adapter，self-deployed
snapshot 则要求 Hugging Face 或 managed-snapshot model、非 subscription runtime 和 managed serving。

Task admission 通过同一 `BEGIN IMMEDIATE` exact 匹配 project/stream、project/workspace snapshots、
execution/capture mode、execution snapshot、materialized context 和 context artifact set，再对当前 generation
固定 revision；且只把下一 generation 持久化为 `required_revision_uncommitted`，不回退到旧 revision。
Activation 后重启允许 required generation 已等于 active generation 的 queued row 保持未 pin，exact retry
重新验证后再原子 pin。Activation 必须在同一写事务内证明所有下一代 queued request 与候选 revision
identity 一致；已经处于 active generation 但仍未 pin 的 queued row 会阻止继续推进 head，直到 exact retry
完成 pin 或该 row 被取消。Store 不会为 activation 静默改写 queued request。Envelope 只含 allowlisted
non-secret identity fields、content-addressed refs 和 opaque IDs；schema 不接受 raw instruction、credential、
env、setup command 或开放 task/runtime/model dict，
非闭集字段在输入边界直接拒绝。Admission request 保存完整 closed envelope identity 字段并自行重算
digest；Task/幂等键不能复用到不同 canonical request，调用方不能直接提交 envelope digest。

Admitted pin 与 terminal state 不可改写。未 pin `cancelled` 是 closed historical audit row：仍验证完整
request sources 和 no-pin 语义，但 head 推进后不再要求其 generation 与当前 active generation 相邻；pinned
`cancelled` 继续验证原 pin 的完整 closure，但不要求原 pin 仍 active。`get_active_revision` 和
`get_task_admission` 都在显式一致 read transaction 中验证对应的完整权威 closure 后返回。Genesis、
queued/admitted/terminal retry 和 terminal transition 都在同一事务使用 active head、revision、
materialization、execution snapshot、完整 envelope/pin 的权威闭包。
Startup 会按 exact schema、canonical manifest/request/snapshot、predecessor chain 和 stream head 做校验。
Store identity、context snapshot/materialization 和 B3 ledger 先只读取 bounded PK 与 SQL octet length并消耗
不可退回的 row/aggregate budget，再以 exact-length guarded `CASE` 逐行取 text、重验 UTF-8 bytes 后解析；
超限单值不会先进入 Python。B3 recovery 只保留 compact identity，context snapshot canonical bytes 另受
独立 aggregate budget。Task admission 非幂等 UPDATE 在写入前按 old/new exact UTF-8 byte delta 检查 row
和 aggregate capacity，写后 readback 重验；exact retry 在容量检查前返回。该保证不扩展到尚未迁移的
legacy job/artifact recovery。

这仍不是完整 B3 生命周期。Execution snapshot persistence 只是内部 canonical identity primitive，不是
B2 verified deployment、serving readiness 或 attestation。当前没有 production issuer，repo-private
testkit seal 只用于测试；#160/B1 verified deployment producer 接入前 release genesis/admission 必须 fail
closed。未来 Core run owner 仍需从 immutable project/workspace snapshots 构造 admission envelope；
Store 已提供严格相邻 successor 的 atomic ledger commit primitive，但 dataset seal、transition、readiness、
serving prepare、调用该 primitive 的 run-owner orchestration、Core HTTP、Gateway/run admission
和 Desktop 状态接线均未实现。因而内部 ledger 或 materializer 的存在都不能被描述为已经完成
端到端 cross-session activation。
其中也包括 public v1 原有的 subscription auth alias 判定；更通用的 `*_subscription` 识别只
存在于 internal projection execution-profile 校验中。
Public legacy artifact read 仍读取 legacy manifest file；immutable `manifest_json` 只由内部
projection 使用，直到后续有显式 versioned public migration。Target 的 `max_artifacts=0`
表示本次 projection 禁用该 target，不调用 handler。
scanner 不支持远程 `hf`/`https`/`s3` inventory，不解压 archives，也不允许把 artifact root 外
的任意 host path 加入信任范围。

HTTP backend 不在 request handler 内训练 LoRA，也不负责 serving inference。Plan-bound
`parametric_memory_sd_lora` job 由 Daemon worker 的固定 trainer service 在 inference 进程外
执行本地 CUDA training，并注册一个 cumulative PEFT adapter；它不接受外部 trainer command
或模型 API endpoint。每代只增加一个 component，冻结旧的全局单位 Frobenius directions，
并在 current trajectories 与 bounded historical replay 上训练新 component 和共享 magnitudes；
magnitude 使用独立学习率，新 component 的 norm 在 generation boundary 被吸收到 magnitude，
因此训练结束、导出和下一代加载保持同一个 effective update。Replay
属于同一个 cumulative state，不是 router 或独立 adapter bank；该语言-agent adaptation 也
因此显式声明不是 upstream paper 的 rehearsal-free equivalent。Parametric memory artifacts 在
context resolve 时以 adapter merge specs 返回给 serving infrastructure。该方法目前是
internal/experimental capability，不属于 External Beta release acceptance。

## OPSD privileged distillation helpers

`openevo.evolution.opsd` 提供官方 OPSD 风格的轻量 helper，用于外部 trainer 或 vLLM
runner 组装 privileged-context distillation 数据流。它不注册 evolution artifact，也不
改变 context resolver 行为。

核心约定：

- student prompt 只包含测试时可见的 problem / schema / state。
- teacher prompt 包含同一个 problem，再额外包含 delimited privileged information。
- completion 必须来自 student on-policy generation。
- teacher 和 student 都 score 同一段 student completion。
- loss mask 只覆盖 completion tokens；prompt 和 privileged block 不参与训练。

使用 vLLM 做 full-logit OPSD 时，外部 runner 应启动允许 all-logits 的 vLLM server
并用同一 tokenizer 构造 student/teacher token sequences。若 teacher/student tokenizer
或 vocab 不一致，只能做 target-token 或 sequence-level distillation，不能做 full-vocab
KL/JSD。

`openevo.evolution.opsd_vllm.VllmOpsdClient` 是一个最小 vLLM/OpenAI-compatible
runner。它会：

1. 用 student model 从 student prompt 生成 on-policy completion。
2. 用 teacher model tokenize teacher privileged prompt。
3. 把同一段 completion token ids 拼到 student/teacher prompt ids 后。
4. 对两段 pre-tokenized input ids 发 `/v1/completions` scoring 请求：
   `max_tokens=0`、`prompt_logprobs=-1`、`return_token_ids=true`、
   `add_special_tokens=false`。
5. 从 vLLM `prompt_logprobs` 中切出 completion token positions，计算 JSD/KL
   smoke loss，或把 logits 交给外部 torch trainer。

vLLM server 需要以允许 full prompt logits 的方式启动，例如设置
`--max-logprobs -1`，并在需要 raw logits 而不是 logprobs 时设置对应的
`--logprobs-mode`。

运行 plan-bound reference worker 时使用同一份 lock：

<!-- openevo:maintainer-only-command -->
```sh {.openevo-maintainer-only}
python -m openevo.evolution.cli worker \
  --base-url http://127.0.0.1:8200 --once \
  --framework-lock /path/to/framework-lock.json
```

`/v1/planned-jobs` 是 Core experiment 的产品路径。它把 immutable plan、method identity、
ordered input snapshots 和 execution envelope 绑定到现有 job/lease lifecycle。`/v1/jobs`
暂时只供尚未迁移的 benchmark automation 使用，不能作为 plan-bound dispatch fallback。
同一 plan/target 的重复 create 是幂等的。Plan-bound claim 必须携带 verified method identity
digests；store 在发 lease 前校验 persisted contract，并在 complete 时拒绝未声明 output type。

## Benchmark automation boundary

Terminal Bench-specific I/O, Harbor execution, reporting, orchestration, and
maintainer commands live in the standalone `benchmarks/terminal_bench/` package.
Core exposes only the backend contracts consumed by that package; it does not ship
benchmark modules, task lists, scorers, or command aliases.

See `benchmarks/terminal_bench/README.md` for installation and command examples.
The standalone entrypoint is `openevo-terminal-bench`; old
`python -m openevo.evolution.cli terminal-bench-*` invocations are unsupported.
