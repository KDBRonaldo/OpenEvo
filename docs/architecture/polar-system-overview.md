# Polar 系统总览

Polar 是面向真实 agent harness 的 rollout-as-a-service 框架。它通过在
准备好的 runtime 中启动 agent，并在 agent 与 inference server 之间放置
gateway proxy，使 agent 尽量不需要为 Polar 改代码。proxy 会捕获模型调用，
并把这些调用转成 RL training 可消费的 trajectory。

## 组件图

```mermaid
flowchart TB
    subgraph ClientSide["Client / Trainer 侧"]
        Trainer[Trainer 或实验驱动]
        Submit[polar submit / rollout API]
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

## 主要模块

| 区域 | 文件 | 职责 |
|---|---|---|
| Gateway server | `src/polar/gateway/server.py` | FastAPI app、proxy route、session/admin endpoints |
| Gateway execution | `src/polar/gateway/node.py` | runtime 生命周期、harness setup/run、post-run、evolution hooks |
| Dispatch | `src/polar/gateway/dispatcher.py` | 分阶段 worker pools 和 session transitions |
| Sessions | `src/polar/gateway/session.py` | 内存 session registry 和 session id 解析 |
| Proxy client | `src/polar/gateway/proxy.py` | 到 inference backend 的 HTTP client，支持 pause/resume generation |
| Engine strategy | `src/polar/gateway/engine.py` | SGLang/vLLM 请求与响应差异 |
| Agent contract | `src/polar/agent/base.py`, `src/polar/agent/models.py` | harness API 和 task schema |
| Presets | `src/polar/agent/presets/` | Codex、Claude Code、OpenHands 等 launcher |
| Trajectory | `src/polar/trajectory/` | completion records 到 trajectory/evaluation 数据 |

## 扩展点

- 新 agent：实现 `BaseHarness` 子类，或使用 `agent.harness: shell`。
- 新 inference backend：新增 `InferenceEngine` 实现，并在 `get_engine` 中注册。
- 新 API 请求/响应形态：在 `src/polar/gateway/transform/` 中添加 transformer。
- 新 trajectory builder/evaluator：通过现有 registry 注册。
- 新 skill/memory/agent-system evolution 方法：通过 Evolution Backend method
  registry 添加，详见 [Reference Evolution Worker](reference-evolution-worker.md)。
