# Polar Evolution Backend

The Polar Evolution Backend is an asynchronous control plane for skill and memory evolution. It receives Polar session and task events, builds datasets from those events, leases jobs to external workers, registers produced artifacts, and resolves context for future Polar sessions.

Start it locally with:

```sh
uv run polar-evolution serve --host 127.0.0.1 --port 8200
```

By default, backend state is stored under `.polar_evolution/`.

Core APIs:

- `/v1/events`
- `/v1/datasets`
- `/v1/jobs`
- `/v1/jobs/claim`
- `/v1/jobs/{job_id}/heartbeat`
- `/v1/jobs/{job_id}/complete`
- `/v1/jobs/{job_id}/fail`
- `/v1/contexts/resolve`

This backend does not train LoRA adapters or serve inference. Parametric memory artifacts are registered with the backend and returned as adapter merge specs for trainer and inference infrastructure.
