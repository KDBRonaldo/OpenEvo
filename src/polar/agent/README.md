# Agent Harnesses

`polar.agent` defines how Polar launches an agent inside a prepared runtime.
The public task field is `agent`, validated by `models.AgentSpec`.

## Main Files

- `base.py`: base harness contract.
- `models.py`: `AgentSpec`, `MCPServerSpec`, and `AgentRunResult`.
- `factory.py`: built-in harness lookup and custom import loading.
- `harnesses/`: implementations for `claude_code`, `codex`, `gemini_cli`,
  `opencode`, `openhands_sdk`, `pi`, `qwen_code`, and `shell`.

## Built-In Harnesses

API type names match `polar.gateway.detection.APIType`: `anthropic`,
`openai_chat`, `openai_responses`, and `google`. `require_streaming` describes the
request style the harness sends to/from the Polar gateway. Package versions are
verified external CLI/SDK releases for harnesses that need one; examples may
choose their own pins or `latest`.

| Harness | API type | require_streaming | Package version |
|---|---|---|---|
| `claude_code` | `anthropic` | `true` | `@anthropic-ai/claude-code@2.1.116` |
| `codex` | `openai_responses` | `true` | `@openai/codex@0.122.0` |
| `gemini_cli` | `google` | `true` | `@google/gemini-cli@0.38.1` |
| `opencode` | `openai_chat` | `true` | `opencode-ai@1.14.19` |
| `openhands_sdk` | `openai_chat` | `false` | `openhands-sdk==1.18.0` |
| `pi` | `openai_chat` | `false` | `@mariozechner/pi-coding-agent@0.67.68` |
| `qwen_code` | `openai_chat` | `true` | `@qwen-code/qwen-code@0.14.5` |
| `shell` | chosen by `agent.custom_shell` | chosen by `agent.custom_shell` | chosen by `agent.custom_shell` |

`shell` is built in as an escape hatch. Use this for your wrapped agents as execution commands.

## Harness Contract

A harness receives the task instruction, runtime execution helper, model name,
environment, settings, and optional MCP server definitions. It returns an
`AgentRunResult` with `completed`, `failed`, or `timeout`.

Harnesses are responsible for starting the agent process. Polar is responsible
for runtime setup, model proxy endpoints, completion capture, and evaluation.

## Adding A Harness

Use one of two paths:

- Add a built-in harness under `harnesses/` and register it in `factory.py`.
- Keep the code outside Polar and set `agent.import_path` in the task.

The import path should resolve to a harness class that follows the base
contract.

## Shell Harness

The `shell` harness is for simple commands or custom wrappers. It requires
`agent.custom_shell` and cannot be combined with MCP servers or skills paths.
