# Gateway Service

`polar.gateway` runs sessions on worker hosts. A gateway accepts dispatches from
the rollout server, prepares a runtime, runs an agent harness, proxies model
requests, builds a trajectory, and returns the terminal result.

## Main Files

- `server.py`: FastAPI app and gateway endpoints.
- `node.py`: gateway node lifecycle.
- `dispatcher.py`: INIT, READY, RUNNING, and POSTRUN stage orchestration.
- `session.py`: session state and status accounting.
- `proxy.py`: OpenAI-compatible proxy surface used by agent harnesses.
- `storage.py`: completion record storage.
- `detection.py`: request API-family detection.
- `transform/`: request and response transformers.

## Responsibilities

- Register with the rollout server and send heartbeats.
- Keep runtime preparation, active generation, and post-run work in separate
  worker pools.
- Capture normalized completion records from proxied model calls.
- Build and evaluate trajectories after the agent exits.
- Call back to the rollout server with `SessionResult`.

## Pause And Resume

The gateway exposes controls used by training bridges to pause or resume model
generation. This lets a trainer stop new generation while weights are being
updated, then resume when the backend is ready.
