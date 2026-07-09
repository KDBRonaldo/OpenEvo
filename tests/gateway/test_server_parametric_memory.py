from __future__ import annotations

from types import SimpleNamespace

from openevo.gateway.engine import SGLangEngine, VLLMEngine
from openevo.gateway.server import _apply_session_adapter_merge_spec


def test_apply_session_adapter_merge_spec_uses_top_level_session_metadata() -> None:
    request = {"model": "Qwen/Qwen3.6-27B", "messages": []}
    session_info = SimpleNamespace(
        metadata={
            "adapter_merge_spec": {
                "merge_mode": "runtime_lora",
                "adapters": [{"adapter_id": "parser-memory"}],
            }
        }
    )

    _apply_session_adapter_merge_spec(
        request,
        session_info=session_info,
        engine=SGLangEngine(),
    )

    assert request["model"] == "Qwen/Qwen3.6-27B:parser-memory"


def test_apply_session_adapter_merge_spec_accepts_nested_evolution_metadata() -> None:
    request = {"model": "Qwen/Qwen3.6-27B", "messages": []}
    session_info = SimpleNamespace(
        metadata={
            "evolution": {
                "adapter_merge_spec": {
                    "merge_mode": "runtime_lora",
                    "adapters": [{"adapter_id": "parser-memory"}],
                }
            }
        }
    )

    _apply_session_adapter_merge_spec(
        request,
        session_info=session_info,
        engine=VLLMEngine(),
    )

    assert request["model"] == "parser-memory"
