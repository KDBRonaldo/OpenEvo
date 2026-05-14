#!/usr/bin/env bash
# Single-entry launcher for the full SWE-Gym Slime GRPO example.
#
# This script bootstraps the pieces that are safe to automate on a cluster:
# external checkouts, local editable installs, Slime/SGLang patches, SWE-Gym
# train JSONL data, shared agent CLI assets, base runtime images, Megatron weight
# conversion, and finally the Polar + Slime training run.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
cd "${PROJECT_ROOT}"

PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python3}"
if [ ! -x "${PYTHON_BIN}" ]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi
SLIME_DIR="${SLIME_DIR:-${PROJECT_ROOT}/slime}"
SLIME_REPO="${SLIME_REPO:-https://github.com/THUDM/slime.git}"
SLIME_REF="${SLIME_REF:-v0.2.4}"

MEGATRON_DIR="${MEGATRON_DIR:-${PROJECT_ROOT}/Megatron-LM}"
MEGATRON_REPO="${MEGATRON_REPO:-https://github.com/NVIDIA/Megatron-LM.git}"
MEGATRON_REF="${MEGATRON_REF:-main}"

HF_CHECKPOINT="${HF_CHECKPOINT:-Qwen/Qwen3.5-4B}"
REF_LOAD="${REF_LOAD:-${TORCH_DIST_DIR:-${PROJECT_ROOT}/tmp/checkpoints/Qwen3.5-4B_torch_dist}}"
TORCH_DIST_DIR="${TORCH_DIST_DIR:-${REF_LOAD}}"
if [ -n "${WANDB_RUN_ID:-}" ]; then
    SAVE_DIR="${SAVE_DIR:-${PROJECT_ROOT}/tmp/ckpt/swegym_slime_grpo_qwen35_4b/${WANDB_RUN_ID}}"
fi
SAVE_DIR="${SAVE_DIR:-${PROJECT_ROOT}/tmp/ckpt/swegym_slime_grpo_qwen35_4b}"
AGENT_CLI_DIR="${AGENT_CLI_DIR:-${PROJECT_ROOT}/tmp/swegym_agent_cli/opt_node}"
APPTAINER_IMAGE_DIR="${APPTAINER_IMAGE_DIR:-${PROJECT_ROOT}/tmp/swegym_apptainer_images}"
APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-${PROJECT_ROOT}/tmp/apptainer_cache}"
APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-${PROJECT_ROOT}/tmp/apptainer_tmp}"
POLAR_APPTAINER_BIN="${POLAR_APPTAINER_BIN:-/usr/bin/apptainer}"

INSTALL_EDITABLE="${INSTALL_EDITABLE:-1}"
APPLY_SGLANG_PATCH="${APPLY_SGLANG_PATCH:-1}"
PREPARE_IMAGES="${PREPARE_IMAGES:-1}"
APPTAINER_PREPARE_JOBS="${APPTAINER_PREPARE_JOBS:-2}"
CONVERT_WEIGHTS="${CONVERT_WEIGHTS:-auto}"
MONITOR_GPU="${MONITOR_GPU:-0}"
TRAIN_GPUS="${TRAIN_GPUS:-0,1}"
ROLLOUT_GPUS="${ROLLOUT_GPUS:-2,3,4,5,6,7}"
export APPTAINER_CACHEDIR APPTAINER_TMPDIR POLAR_APPTAINER_BIN

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command not found: $1" >&2
        exit 1
    fi
}

clone_if_missing() {
    local name="$1"
    local repo="$2"
    local ref="$3"
    local dest="$4"
    if [ -d "${dest}/.git" ]; then
        echo "${name} checkout exists: ${dest}"
        return
    fi
    if [ -e "${dest}" ]; then
        echo "ERROR: ${name} path exists but is not a git checkout: ${dest}" >&2
        exit 1
    fi
    echo "Cloning ${name} ${ref} -> ${dest}"
    git clone --branch "${ref}" --depth 1 "${repo}" "${dest}"
}

checkpoint_ready() {
    [ -f "${REF_LOAD}/latest_checkpointed_iteration.txt" ]
}

maybe_login_wandb() {
    if [ -z "${WANDB_API_KEY:-}" ]; then
        return
    fi
    "${PYTHON_BIN}" - <<'PY'
import os

try:
    import wandb
except Exception:
    raise SystemExit(0)

key = os.environ.get("WANDB_API_KEY")
if key and hasattr(wandb, "login"):
    wandb.login(key=key, relogin=True)
PY
}

require_cmd git
require_cmd "${PYTHON_BIN}"
require_cmd uv
require_cmd "${POLAR_APPTAINER_BIN}"
require_cmd ray

clone_if_missing "Slime" "${SLIME_REPO}" "${SLIME_REF}" "${SLIME_DIR}"
clone_if_missing "Megatron-LM" "${MEGATRON_REPO}" "${MEGATRON_REF}" "${MEGATRON_DIR}"

if [ "${INSTALL_EDITABLE}" = "1" ]; then
    uv pip install -e .
    uv pip install -e "${SLIME_DIR}"
    uv pip install -e "${MEGATRON_DIR}"
fi

bash "${PROJECT_ROOT}/scripts/patch/patch_slime.sh" "${SLIME_DIR}"
if [ "${APPLY_SGLANG_PATCH}" = "1" ]; then
    bash "${PROJECT_ROOT}/scripts/patch/patch_sglang.sh"
fi

"${PYTHON_BIN}" "${SCRIPT_DIR}/prepare_data.py"

if [ "${PREPARE_IMAGES}" = "1" ]; then
    "${PYTHON_BIN}" "${SCRIPT_DIR}/prepare_apptainer_images.py" \
        --agent-cli-dir "${AGENT_CLI_DIR}" \
        --image-dir "${APPTAINER_IMAGE_DIR}" \
        --cache-dir "${APPTAINER_CACHEDIR}" \
        --tmp-dir "${APPTAINER_TMPDIR}" \
        --jobs "${APPTAINER_PREPARE_JOBS}"
fi

if [ "${CONVERT_WEIGHTS}" = "1" ] || { [ "${CONVERT_WEIGHTS}" = "auto" ] && ! checkpoint_ready; }; then
    HF_CHECKPOINT="${HF_CHECKPOINT}" \
    TORCH_DIST_DIR="${TORCH_DIST_DIR}" \
    SLIME_DIR="${SLIME_DIR}" \
    MEGATRON_DIR="${MEGATRON_DIR}" \
        bash "${SCRIPT_DIR}/convert_weights.sh"
fi

maybe_login_wandb

MONITOR_PID=""
if [ "${MONITOR_GPU}" = "1" ]; then
    uv run python "${PROJECT_ROOT}/scripts/monitor_wandb_gpu.py" \
        --no-wandb \
        --train-gpus "${TRAIN_GPUS}" \
        --rollout-gpus "${ROLLOUT_GPUS}" &
    MONITOR_PID="$!"
fi

cleanup() {
    if [ -n "${MONITOR_PID}" ]; then
        kill "${MONITOR_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

HF_CHECKPOINT="${HF_CHECKPOINT}" \
REF_LOAD="${REF_LOAD}" \
TORCH_DIST_DIR="${TORCH_DIST_DIR}" \
SAVE_DIR="${SAVE_DIR}" \
PYTHON_BIN="${PYTHON_BIN}" \
SLIME_DIR="${SLIME_DIR}" \
MEGATRON_DIR="${MEGATRON_DIR}" \
AGENT_CLI_DIR="${AGENT_CLI_DIR}" \
APPTAINER_IMAGE_DIR="${APPTAINER_IMAGE_DIR}" \
    bash "${SCRIPT_DIR}/run.sh"
