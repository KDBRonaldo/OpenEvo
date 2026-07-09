from __future__ import annotations

from types import SimpleNamespace

from slime_bridge.rollout import _openevo_extra_metrics


def _sample(
    session_id: str,
    reward: float,
    *,
    status: str = "COMPLETED",
    placeholder: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        reward={"score": reward},
        metadata={
            "openevo": {
                "session_id": session_id,
                "session_status": status,
                "placeholder": placeholder,
                "timing": {
                    "register_to_init_queue_ms": 1.0,
                    "init_ms": 2.0,
                    "run_ms": 3.0,
                    "postrun_ms": 4.0,
                },
            },
        },
    )


def test_openevo_reward_mean_completed_uses_unique_non_placeholder_sessions() -> None:
    samples = [
        _sample("completed-1", 1.0),
        _sample("completed-1", 1.0),
        _sample("completed-2", 0.0),
        _sample("timeout-1", 0.0, status="TIMEOUT", placeholder=True),
        _sample("empty-completed", 0.0, placeholder=True),
    ]
    metrics = _openevo_extra_metrics(
        samples,
        rewards=[1.0, 1.0, 0.0, 0.0, 0.0],
        reward_key="score",
    )

    assert metrics["openevo/reward_mean"] == 0.4
    assert metrics["openevo/reward_mean_completed"] == 0.5
    assert metrics["openevo/rollout_success_rate"] == 0.5
