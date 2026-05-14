# Slime Bridge

`slime_bridge` connects Slime rollout calls to a running Polar rollout service.
It is intentionally kept outside the core `polar` package namespace because
Slime, Ray, Megatron, and training dependencies are installed separately.

## Main Files

- `config.py`: bridge configuration helpers.
- `rollout.py`: async worker lifecycle, task submission, policy update
  coordination, and Slime-facing rollout functions.
- `_messages.py`: prompt and message conversion.
- `adapter.py`: conversion from Polar session results to Slime-like samples.
- `data_source.py`: Slime data source integration.
- `reward.py`: reward helpers.
- `reward_post_process.py`: reward normalization after rollout.

## What The Bridge Owns

- Convert Slime samples and prompt messages into Polar task requests.
- Submit async batches to `polar_rollout_url`.
- Track rollout ids, policy versions, and scheduler metadata.
- Pause and resume gateway generation during configured weight update windows.
- Convert Polar trajectories back into sample objects expected by Slime.
- Normalize or filter rewards for failed and oversized trajectories.

## Slime Installation

Install Slime from the THUDM git checkout, not from the unrelated PyPI `slime`
package. The SWE-Gym Slime GRPO example uses `launch_e2e.sh` to automate this
setup; the manual equivalent from the repository root is:

```bash
git clone --branch v0.2.4 --depth 1 https://github.com/THUDM/slime.git slime
git clone https://github.com/NVIDIA/Megatron-LM.git Megatron-LM

uv pip install -e .
uv pip install -e slime
uv pip install -e Megatron-LM

bash scripts/patch/patch_slime.sh slime
```

Use `SLIME_DIR=/path/to/slime` and `MEGATRON_DIR=/path/to/Megatron-LM` when
working with existing checkouts outside the repository root.

The Slime training environment is expected to provide heavy dependencies such
as `torch`. Polar does not add those dependencies for the first beta release.
