# ProRLAgent Server: A Scalable Multi-turn Rollout Infrastructure for RL Agents Training

<p align="center"><img src="NVIDIA_Assets/logo.png" alt="logo" width="400" /></p>

<p align="center">
<a href="https://codecov.io/gh/NVIDIA-NeMo/ProRL-Agent-Server"><img src="https://codecov.io/gh/NVIDIA-NeMo/ProRL-Agent-Server/graph/badge.svg?token=2F1UIV9HW6" alt="codecov" /></a>
<a href="https://www.python.org/downloads/release/python-3100/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+" /></a>
<a href="https://github.com/NVIDIA-NeMo/ProRL-Agent-Server/stargazers/"><img src="https://img.shields.io/github/stars/NVIDIA-NeMo/ProRL-Agent-Server.svg?style=social&label=Star" alt="GitHub Stars" /></a>
</p>

## ☁️ Introduction

ProRLAgent Server is a scalable multi-turn rollout system for training and evaluating RL agents. Built on top of OpenHands, it offers high concurrency and a pluggable handler interface to support diverse agent tasks.

- **Decoupled RL Training & Rollouts:** rollouts run as a service; any RL trainer can consume the outputs.
- **High concurrency:**  execute large-scale jobs with LLM load balancing.
- **Pluggable AgentHandler:**  customize for different tasks and agents.
- **Lifecycle management:**  built-in support for status tracking, queuing, timeouts, and cleanup.
- **Token-in / Token-out:** communicate in tokens to maintain turn alignment and ensure stable training.
- **Singularity runtime:** rootless execution with single-file containers (.sif), seamless Slurm integration, secure multi-user support.
- **Efficient Bash tool:** ptyprocess-based implementation for 6x speed improvements over tmux-based approach.
- **Efficient IPython tool:** direct IPython kernel integration without network overhead.
- **UDS communication:** Unix domain sockets for better throughput and isolation.

## 💻 Quick Start

1) **Install dependencies**

- Install OpenHands Dependencies

```bash
poetry install --with dev,test,runtime,evaluation
pip install git+https://github.com/SWE-Gym/SWE-Bench-Package.git
pip install git+https://github.com/R2E-Gym/R2E-Gym.git
```

- Install Singularity/Apptainer Sandbox 

```bash
sudo apt-get update
sudo apt-get install -y software-properties-common curl gnupg
sudo apt-get install -y singularity-container fuse
sudo add-apt-repository -y ppa:apptainer/ppa
sudo apt-get update
sudo apt-get install -y apptainer
```
2) **Start the VLLM server with your desired Hugging Face model:**

```bash
vllm serve path/to/your/model --enable-auto-tool-choice --tool-call-parser hermes  --host 127.0.0.1 --port 8000 --api-key key --served-model-name model_name &
```

Replace `path/to/your/model` with the actual path to your Hugging Face model. Set up the server IP, Port, and model name.


3) **Pull singularity sandboxs for swe tasks**

```bash
python scripts/pull_swe_images.py --parquet-file /path/to/train.parquet --dest-dir /some/dir --temp-base /some/dir --log-name log
```

Download parquet data from Huggingface. Supported Training data:

- swe-gym: https://huggingface.co/datasets/NovaSky-AI/SkyRL-v0-293-data
- r2egym: https://huggingface.co/R2E-Gym
- swe-bench-multimodal: https://huggingface.co/datasets/SWE-bench/SWE-bench_Multimodal
- swe-bench: https://huggingface.co/datasets/SWE-bench/SWE-bench
- swe-smith: https://huggingface.co/datasets/SWE-bench/SWE-smith

4) **Start the async evaluation server (FastAPI)**

This command starts the FastAPI-based async evaluation server and listens on the given host/port.
It exposes /start, /process, and /status endpoints, and uses --max-init-workers/--max-run-workers and --timeout to control concurrency and time limits.

```bash
python scripts/start_server.py --host 0.0.0.0 --port 8006 --max-init-workers 64 --max-run-workers 64 --timeout 300
```

5) **Test the server (HTTP I/O)**

Before sending jobs to `/process`, make sure you follow this sequence (assumes you already started a VLLM server in step 2):

1. Register at least one LLM server address (include `/v1`):
```bash
curl -X POST http://localhost:8006/add_llm_server \
  -H 'Content-Type: application/json' \
  -d '{"address":"http://127.0.0.1:8000/v1"}'
```

2. Start the worker process:
```bash
curl -X POST http://localhost:8006/start
```

3. (Optional) Check status:
```bash
curl http://localhost:8006/status
```

Notes:
- You can call `/add_llm_server` before `/start`; the address will be buffered and applied when the worker starts.
- Ensure the `sampling_params.model` and `api_key` in your request match the model name and key you used when launching VLLM in step 2.

**Option 1: Quick test using the built-in script**

```
python scripts/tests/test_server.py
```

**Option 2: Test using curl**

Quick try: send a task to `/process` and read the JSON result.

Input (request body):
- `instance`: the task info (must include `data_source` and any fields your handler needs)
- `sampling_params`: optional LLM/agent settings (e.g., `temperature`, `top_p`, `max_tokens`)
- `job_id` (optional): your own identifier

Example:

```bash
curl -X POST http://localhost:8006/process \
  -H 'Content-Type: application/json' \
  -d '{
    "instance": {
      "data_source": "swebench",
      "instance_id": "python__mypy-16203",
      "trajectory_id": "t0",
      "patch": "",
      "metadata": {}
    },
    "sampling_params": {
      "model": "hosted_vllm/Qwen2.5-7B-Instruct",
      "api_key": "key",
      "modify_params": false,
      "log_completions": true,
      "native_tool_calling": false,
      "temperature": 0.6,
      "top_p": 0.9,
      "token_level_generation": true,
      "custom_tokenizer": "tokenizer_path",
      "max_iterations": 5
    }
  }'
```

Output (response body):

```json
{
  "resolved": true,
  "report": {"pass@1": 0.0, "details": {"...": "..."}},
  "timing": {"init": 2.1, "run": 41.3, "eval": 5.2, "others": 1.4, "timeout": 300.0}
}
```

## 💻 Add a New Task/Handler

To add a new task:

- Implement an `AgentHandler` with `name`, `init(job_details, ...)`, `run(job_details, ...)`, and `eval(job_details, ...)`.
- Register it in the registry so that `instance["data_source"] == name` routes requests to your handler.
- Provide a `final_result(job_details)` function for result shaping.
- Ensure your handler returns a consistent result schema and handles timeouts/errors.

Minimal sketch:

```python
from openhands.nvidia.registry import AgentHandler, register_agent_handler

class MyTaskHandler(AgentHandler):
    @property
    def name(self) -> str: return "my_task"
    async def init(self, job_details, sid=None, **kwargs):
        return runtime, metadata, config
    async def run(self, job_details, sid=None, **kwargs):
        return {"git_patch": "...", "messages": []}
    async def eval(self, job_details, sid=None, allow_skip=True, reward=None):
        return {"report": {"resolved": True}}

register_agent_handler(MyTaskHandler())
```

Then submit requests with `{"data_source": "my_task", ...}` in the `instance`.

## 💻 Run unit tests

Example:
```bash
TEST_RUNTIME=singularity RUN_AS_OPENHANDS=False PYTHONPATH='.' pytest tests/runtime/test_browsing.py -v -s
```

### Important Environment Variables

#### Image Storage Location
**`OH_RUNTIME_SINGULARITY_IMAGE_REPO`** - Specifies the directory where Singularity runtime images will be stored.
```bash
OH_RUNTIME_SINGULARITY_IMAGE_REPO=/path/to/singularity_images
```

## 📄 Documentation

More module READMEs (click to open):

- [`openhands/README.md`](openhands/README.md)
- [`openhands/nvidia/README.md`](openhands/nvidia/README.md)
- [`openhands/llm/nvidia/README.md`](openhands/llm/nvidia/README.md)
- [`scripts/README.md`](scripts/README.md)
- [`tests/nvidia/README.md`](tests/nvidia/README.md)

## 💡 Current Results
<img src="NVIDIA_Assets/learning_curve.png" alt="learning curve" width="600" />

To validate the functionality of the ProRLAgent servers, we conducted proof-of-concept experiments on software engineering (SWE) tasks by integrating the server with the Verl reinforcement learning (RL) framework. Specifically, we used swe-gym along with a subset of the R2E-gym dataset, comprising a total of 800 training instances, to perform GRPO training. Our experiments were carried out on the Qwen3-4B-Instruct model and evaluated on the SWE-Bench-Verified benchmark. The results demonstrate a performance improvement, with accuracy increasing from **15.0%** to **20.4%**.

