# OpenEvo Core Runtime 系统总览

OpenEvo Core 的低层 runtime 是面向真实 agent harness 的 rollout-as-a-service
框架。它通过在
准备好的 runtime 中启动 agent，并在 agent 与 inference server 之间放置
gateway proxy，使 agent 尽量不需要为 OpenEvo 改代码。proxy 会捕获模型调用，
并把这些调用转成 RL training 可消费的 token-level trajectory。对于不经过
OpenEvo proxy 的纯文本 capture 模式，OpenEvo 只保证 transcript-level
trajectory，不伪造 token id、logprob 或 token-level metric。订阅登录只是某些
harness 的 auth 方式；它必须显式开启 transcript capture 后才允许运行。

这个文档描述的是 OpenEvo Core Backend 内部 rollout/gateway/runtime/proxy 数据路径。
公开 runtime contract 使用 OpenEvo identity：`OPENEVO_*`、`/openevo/session`
和 `openevo.session_completed`。

## 组件图

```mermaid
flowchart TB
    subgraph ClientSide["Client / Trainer 侧"]
        Trainer[Trainer 或实验驱动]
        Submit[backend automation / rollout API]
    end

    subgraph RolloutService["Rollout Service"]
        RolloutAPI[FastAPI rollout server]
        Balancer[Gateway balancer]
        TaskState[Task + session 状态]
    end

    subgraph GatewayNode["Gateway Node"]
        GatewayAPI[FastAPI gateway server]
        Dispatcher[SessionDispatcher]
        NodeManager[GatewayNodeManager]
        CompletionStore[CompletionStore + writer]
        Proxy[LLM proxy route]
    end

    subgraph RuntimeBox["单 session Runtime"]
        Runtime[Docker / Apptainer runtime]
        Harness[Agent harness]
        AgentProcess[Agent CLI / SDK / shell]
    end

    subgraph ModelSide["Inference 侧"]
        Engine[InferenceEngine strategy]
        ModelServer[SGLang 或 vLLM server]
    end

    subgraph PostRun["Post-run"]
        Builder[Trajectory builder]
        Evaluator[Evaluator]
        Artifacts[Session artifacts]
    end

    Trainer --> Submit --> RolloutAPI
    RolloutAPI --> Balancer --> GatewayAPI
    GatewayAPI --> Dispatcher --> NodeManager
    NodeManager --> Runtime
    Runtime --> Harness --> AgentProcess
    AgentProcess -->|OpenAI / Anthropic / Google API| Proxy
    Proxy --> Engine --> ModelServer
    Proxy --> CompletionStore
    NodeManager --> Builder --> Evaluator --> GatewayAPI
    GatewayAPI --> RolloutAPI
    NodeManager --> Artifacts
```

## Session 生命周期

```mermaid
stateDiagram-v2
    [*] --> REGISTERED
    REGISTERED --> INITIALIZING: dispatch 被接受
    INITIALIZING --> READY: runtime 启动并完成 prepare
    READY --> RUNNING: 获得 run slot
    RUNNING --> POST_RUN: agent 命令结束或超时
    POST_RUN --> COMPLETED: trajectory 和 evaluation 成功
    POST_RUN --> ERROR: post-run 失败
    INITIALIZING --> ERROR
    RUNNING --> ERROR
    RUNNING --> TIMEOUT
    COMPLETED --> [*]
    ERROR --> [*]
    TIMEOUT --> [*]
```

分阶段生命周期由 `SessionDispatcher` 和 `GatewayNodeManager` 实现。这个拆分
对运行时很重要：同一个 node 可以分别限制 runtime 初始化、活跃 agent 执行和
post-run 工作的并发。

## 请求路径

```mermaid
sequenceDiagram
    participant A as Agent Process
    participant G as Gateway Proxy
    participant T as Transformer
    participant E as InferenceEngine
    participant M as SGLang / vLLM
    participant S as CompletionStore

    A->>G: OpenAI / Anthropic / Google 请求
    G->>G: 从 API key / header / query 解析 session id
    G->>T: 检测 API 类型并转成 OpenAI chat
    T-->>G: OpenAI-compatible 请求
    G->>E: 应用 backend 参数和 adapter spec
    E->>M: /v1/chat/completions
    M-->>E: 模型响应
    E-->>G: 规范化后的响应
    G->>S: 保存原始请求、served 请求、响应
    G->>T: 转回 agent 期望的响应格式
    T-->>A: agent-facing response
```

Streaming 请求会被处理成 synthetic stream：gateway 对 backend 发起一次
non-streaming 请求，再把完整响应格式化成 SSE events 返回给 agent。这样可以让
capture 和 trajectory 构造保持确定性。

## Capture 模式和训练信号

OpenEvo Core 当前区分两类 capture：

| 模式 | 入口 | 产物 | 适合用途 |
|---|---|---|---|
| OpenEvo proxy capture | agent 请求 `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` / Gemini proxy | `CompletionSession`，包含请求、响应、token ids、logprobs | token-level RL、policy gradient、需要 loss mask 的训练 |
| Pure-text transcript capture | `agent.settings.capture_mode="transcript"`；可配合订阅登录或其他不走 proxy 的 harness | `agent_transcript` trajectory，来自 `logs/agent/step.xx.stdout.log` | skill/memory/agent-system evolution、行为回放、非 token-level 评估 |

纯文本 transcript capture 是显式选项，而不是某个 harness 的隐式行为。Gateway
只有在 `agent.settings.capture_mode="transcript"` 或等价 transcript capture mode
开启、且本次 run 没有 proxy completion records 时，才会 fallback 到
`agent_transcript` builder，从 agent stdout transcript 构造 trajectory。这个
trajectory 的 `Trace.response_ids`、`Trace.loss_mask`、`Trace.response_logprobs`
为空，并在 trajectory/trace metadata 中设置：

```json
{
  "capture_mode": "transcript",
  "token_level_metrics_available": false
}
```

后续 skill/memory/agent-system evolution 可以消费这类 transcript trajectory；
token-level RL 训练必须过滤掉 `token_level_metrics_available=false` 的 traces，
或要求任务走 OpenEvo proxy 模式。

如果 harness 选择 `auth_mode="subscription"` 或 harness-specific subscription
alias，它必须同时设置 transcript capture mode。当前 managed subscription release 只接受
literal `capture_mode="transcript"`、exact managed runtime profile/image、Docker host-user
执行，并显式 unset proxy 相关环境变量。其他订阅式 harness 也应遵守同样边界：订阅负责
auth，transcript capture 负责 evolution 可消费的行为记录。

Codex subscription 的 `CODEX_HOME` 由 Core 固定为
`/openevo/credentials/codex`。Gateway 仅在 runtime prepare 完成后，才把经过 no-follow、
owner、regular、link-count-one、size/digest/identity 完整校验的宿主机
`~/.codex/auth.json` staging 到 session tree 外的专用私有 bind mount。Workspace、artifact、
log 和 transcript capture 不得自动复制 auth 原文或已知敏感 leaf values。

## 主要模块

| 区域 | 文件 | 职责 |
|---|---|---|
| Gateway server | `src/openevo/gateway/server.py` | FastAPI app、proxy route、session/admin endpoints |
| Gateway execution | `src/openevo/gateway/node.py` | runtime 生命周期、harness setup/run、post-run、evolution hooks |
| Dispatch | `src/openevo/gateway/dispatcher.py` | 分阶段 worker pools 和 session transitions |
| Sessions | `src/openevo/gateway/session.py` | 内存 session registry 和 session id 解析 |
| Proxy client | `src/openevo/gateway/proxy.py` | 到 inference backend 的 HTTP client，支持 pause/resume generation |
| Engine strategy | `src/openevo/gateway/engine.py` | SGLang/vLLM 请求与响应差异 |
| Agent contract | `src/openevo/harness/base.py`, `src/openevo/harness/models.py` | harness API 和 task schema |
| Presets | `src/openevo/harness/presets/` | Codex、Claude Code、OpenHands 等 launcher |
| Trajectory | `src/openevo/trajectory/` | completion records 到 trajectory/evaluation 数据 |

## 扩展点

- 新 agent：实现 `BaseHarness` 子类，或使用 `agent.harness: shell`。
- 新 inference backend：新增 `InferenceEngine` 实现，并在 `get_engine` 中注册。
- 新 API 请求/响应形态：在 `src/openevo/gateway/transform/` 中添加 transformer。
- 新 trajectory builder/evaluator：通过现有 registry 注册。
- 新 skill/memory/agent-system evolution 方法：通过 Evolution Backend method
  registry 添加，详见
  [Evolution API And Method Integration](evolution-api-and-method-integration.md)。
