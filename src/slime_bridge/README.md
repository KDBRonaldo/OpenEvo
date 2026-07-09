# Slime Bridge

`slime_bridge` connects [Slime](https://github.com/THUDM/slime)'s RL training
loop to a running OpenEvo rollout server over HTTP. It lives **outside** the
`openevo` package because Slime, Ray, Megatron, and torch are installed
separately — OpenEvo depends on none of them.

## How it fits

Slime calls one entry point, `generate_rollout_polar_async`, wired in via
`--rollout-function-path`. That entry point name is a legacy compatibility name;
the behavior is the OpenEvo bridge. From there the bridge:

- submits async task batches to `openevo_rollout_url` (or a node derived from
  `openevo_topology_path`) and collects each result through a local callback
  listener with a polling safety net;
- tracks rollout ids and policy versions, stamps `{group_id, policy_version,
  rollout_step}` onto every task, and **drops groups that drift too far
  off-policy** (`max_off_policy_steps`) while keeping async admission bounded
  (`max_async_level`);
- **pauses/resumes gateway generation** around weight updates (the gateway's
  `/admin/inference/pause` + `/resume`) when overlap is enabled;
- converts each OpenEvo `Trajectory` back into Slime `Sample`s (one per trace,
  grouped so the reward post-processor treats them as one trajectory), dropping
  empty or oversized traces;
- normalizes rewards GRPO-style per group and zeroes out failed/aborted
  trajectories.

## Main files

- `config.py`: `PolarSlimeConfig` + `resolve_polar_slime_config`; these class and
  function names are legacy compatibility names. The resolver accepts
  `openevo_*` config keys first and falls back to old aliases. It also renders the
  task payload, the instruction, and the topology that points gateways at Slime's
  SGLang router.
- `rollout.py`: the async worker (submit → callback/poll → convert), the
  evaluation path, policy-update coordination, the acceptance filters, and the
  Slime entry point.
- `_messages.py`: prompt/message flattening shared by rollout + adapter.
- `adapter.py`: convert an OpenEvo `SessionResult` into Slime `Sample`s.
- `data_source.py`: `CeilEpochRolloutDataSourceWithBuffer` — rounds the epoch
  length up so the dataset tail isn't skipped.
- `reward.py`: reward hook that reads the reward OpenEvo already embedded.
- `reward_post_process.py`: trajectory-aware, group-normalized reward shaping.

## What the bridge owns

- Turn Slime samples + prompts into OpenEvo task requests and submit async batches.
- Track rollout ids / policy versions; drop off-policy-stale groups; bound async
  admission.
- Filter unusable groups (zero trainable tokens, too few completed samples,
  logprob errors) with per-category metrics.
- Pause/resume gateway generation during weight-update windows.
- Convert OpenEvo trajectories back into Slime samples; normalize and zero rewards.
- Run the evaluation path over `eval_datasets` and emit W&B metrics.

## Slime installation

Install Slime from the THUDM git checkout (not the unrelated PyPI `slime`
package). The SWE-Gym Slime GRPO example automates this with `launch_e2e.sh`; the
manual equivalent from the repository root is:

```bash
git clone --branch v0.2.4 --depth 1 https://github.com/THUDM/slime.git slime
git clone https://github.com/NVIDIA/Megatron-LM.git Megatron-LM

uv pip install -e .
uv pip install -e slime
uv pip install -e Megatron-LM

bash scripts/patch/patch_slime.sh slime
```

Use `SLIME_DIR=/path/to/slime` and `MEGATRON_DIR=/path/to/Megatron-LM` for
checkouts outside the repository root. The Slime training environment provides
the heavy dependencies (e.g. `torch`); OpenEvo does not add them.
