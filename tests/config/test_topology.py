from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from polar.config import TopologyConfig


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


def test_topology_defaults_public_urls_and_rollout_url(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "rollout": {
                "host": "0.0.0.0",
                "port": 8080,
            },
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "host": "0.0.0.0",
                        "port": 8100,
                    }
                ],
            },
        },
    )

    topology = TopologyConfig.load(path)

    assert topology.rollout.public_url == "http://127.0.0.1:8080"
    assert topology.gateway.nodes[0].public_url == "http://127.0.0.1:8100"
    assert topology.gateway.rollout_server_url == topology.rollout.public_url


def test_topology_rejects_unknown_keys(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "rollout": {"unexpected": True},
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "public_url": "http://127.0.0.1:8100",
                    }
                ],
            },
        },
    )

    with pytest.raises(ValueError, match="unexpected"):
        TopologyConfig.load(path)


def test_select_gateway_requires_node_id_for_multi_node_topology(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {"id": "node-a", "public_url": "http://127.0.0.1:8100"},
                    {"id": "node-b", "public_url": "http://127.0.0.1:8101"},
                ],
            },
        },
    )
    topology = TopologyConfig.load(path)

    with pytest.raises(ValueError, match="--node-id"):
        topology.select_gateway_node()

    assert topology.select_gateway_node("node-b").port == 8081


def test_inference_block_selects_engine_and_base_url(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "public_url": "http://127.0.0.1:8100",
                        "inference": {"engine": "vllm", "base_url": "http://127.0.0.1:8000"},
                    }
                ],
            },
        },
    )
    node = TopologyConfig.load(path).gateway.nodes[0]
    assert node.engine == "vllm"
    assert node.inference_base_url == "http://127.0.0.1:8000"


def test_inference_engine_defaults_to_sglang(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "public_url": "http://127.0.0.1:8100",
                        "inference": {"base_url": "http://127.0.0.1:8000"},
                    }
                ],
            },
        },
    )
    node = TopologyConfig.load(path).gateway.nodes[0]
    assert node.engine == "sglang"


def test_inference_defaults_when_block_omitted(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {"gateway": {"nodes": [{"id": "node-a", "public_url": "http://127.0.0.1:8100"}]}},
    )
    node = TopologyConfig.load(path).gateway.nodes[0]
    assert node.engine == "sglang"
    assert node.inference_base_url == "http://127.0.0.1:8000"


def test_invalid_inference_engine_is_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "public_url": "http://127.0.0.1:8100",
                        "inference": {"engine": "tgi", "base_url": "http://127.0.0.1:8000"},
                    }
                ],
            },
        },
    )
    with pytest.raises(ValueError):
        TopologyConfig.load(path)


def test_invalid_inference_base_url_is_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "public_url": "http://127.0.0.1:8100",
                        "inference": {"engine": "vllm", "base_url": "not-a-url"},
                    }
                ],
            },
        },
    )
    with pytest.raises(ValueError, match="inference.base_url"):
        TopologyConfig.load(path)


def test_legacy_sglang_block_is_rejected(tmp_path: Path) -> None:
    # No backward compatibility: the old `sglang:` block is now an unknown key.
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {
                        "id": "node-a",
                        "public_url": "http://127.0.0.1:8100",
                        "sglang": {"base_url": "http://127.0.0.1:8000"},
                    }
                ],
            },
        },
    )
    with pytest.raises(ValueError, match="sglang"):
        TopologyConfig.load(path)


def test_duplicate_gateway_node_ids_are_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [
                    {"id": "node-a", "public_url": "http://127.0.0.1:8100"},
                    {"id": "node-a", "public_url": "http://127.0.0.1:8101"},
                ],
            },
        },
    )

    with pytest.raises(ValueError, match="Duplicate gateway node id"):
        TopologyConfig.load(path)


def test_topology_loads_optional_evolution_config(tmp_path: Path) -> None:
    config_path = tmp_path / "topology.yaml"
    config_path.write_text(
        """
rollout:
  host: 127.0.0.1
  port: 8080
gateway:
  nodes:
    - id: node-a
      host: 127.0.0.1
      port: 8100
      model_served: Qwen/Qwen3.6-27B
      inference:
        engine: vllm
        base_url: http://127.0.0.1:8000
evolution:
  enabled: true
  backend_url: http://127.0.0.1:8200
  context:
    target_dir: /polar/session/evolution
    timeout_seconds: 3
    fail_open: true
  event_export:
    enabled: true
    timeout_seconds: 4
    fail_open: true
""".strip()
    )

    topology = TopologyConfig.load(config_path)

    assert topology.evolution is not None
    assert topology.evolution.enabled is True
    assert topology.evolution.backend_url == "http://127.0.0.1:8200"
    assert topology.evolution.context.target_dir == "/polar/session/evolution"
    assert topology.evolution.context.timeout_seconds == 3
    assert topology.evolution.event_export.timeout_seconds == 4


def test_invalid_evolution_backend_url_is_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [{"id": "node-a", "public_url": "http://127.0.0.1:8100"}]
            },
            "evolution": {"backend_url": "not-a-url"},
        },
    )

    with pytest.raises(ValueError, match="evolution.backend_url"):
        TopologyConfig.load(path)


def test_blank_evolution_context_target_dir_is_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [{"id": "node-a", "public_url": "http://127.0.0.1:8100"}]
            },
            "evolution": {"context": {"target_dir": "   "}},
        },
    )

    with pytest.raises(ValueError, match="evolution.context.target_dir"):
        TopologyConfig.load(path)


def test_unknown_evolution_context_key_is_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path / "topology.yaml",
        {
            "gateway": {
                "nodes": [{"id": "node-a", "public_url": "http://127.0.0.1:8100"}]
            },
            "evolution": {"context": {"unexpected": True}},
        },
    )

    with pytest.raises(ValueError, match="unexpected"):
        TopologyConfig.load(path)
