# Configuration

`polar.config` loads one topology file that describes the rollout server and
all gateway nodes.

## Main Files

- `topology.py`: Pydantic models for rollout and gateway configuration.
- `__init__.py`: package exports.

## Topology Schema

The top-level fields are:

- `rollout`: host, port, public URL, save directory, dispatch polling, and
  callback timing.
- `gateway`: heartbeat interval, optional rollout URL override, and gateway
  node list.
- `gateway.nodes[]`: node id, host, port, public URL, served model name, worker
  limits, SGLang endpoint, and optional default runtime.

Unknown keys are rejected so removed or misspelled options fail early.

## Example Topology

A topology file declares one rollout server and one or more gateway nodes:

```yaml
rollout:
  host: 127.0.0.1
  port: 8080
  public_url: http://127.0.0.1:8080
  save_dir: ./rollout_results

gateway:
  heartbeat_interval_seconds: 30
  nodes:
    - id: localhost-node-01
      host: 127.0.0.1
      port: 8100
      public_url: http://127.0.0.1:8100
      model_served: Qwen/Qwen3.5-4B
      max_init_workers: 8
      max_run_workers: 4
      max_postrun_workers: 4
      sglang:
        base_url: http://127.0.0.1:8000
```

## Public URL Rules

`public_url` values must be reachable by the caller:

- The rollout server calls each gateway node's `public_url`.
- Gateway nodes call the rollout server callback URL.
- Each gateway calls its configured `sglang.base_url`.

When `public_url` is omitted or empty, Polar derives a local URL from host and
port. For multi-node deployments, set explicit reachable URLs.

## Multi-Node Selection

`polar serve_gateway` needs `--node-id` when the topology contains more than one
gateway node. This prevents a gateway process from accidentally starting with
the wrong SGLang endpoint or worker limits.
