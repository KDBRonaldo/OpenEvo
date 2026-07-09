"""Reward adapter for Slime custom reward model hooks."""

from __future__ import annotations

from typing import Any


def _arg_value(args: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(args, name):
            value = getattr(args, name)
            if value is not None:
                return value
    return default


async def reward_func(args: Any, sample_or_samples: Any, **kwargs: Any) -> Any:
    """Read the reward already embedded in OpenEvo-converted Slime samples."""
    del kwargs
    reward_key = str(
        _arg_value(args, "openevo_reward_key", "polar_reward_key", "reward_key", default="score")
    )
    if isinstance(sample_or_samples, list):
        return [{reward_key: _extract_reward(sample, reward_key)} for sample in sample_or_samples]
    return {reward_key: _extract_reward(sample_or_samples, reward_key)}


def _extract_reward(sample: Any, reward_key: str) -> float:
    reward = getattr(sample, "reward", None)
    if isinstance(reward, dict):
        if reward_key in reward:
            return float(reward[reward_key])
        if "score" in reward:
            return float(reward["score"])
        for value in reward.values():
            if isinstance(value, (int, float)):
                return float(value)
        return 0.0
    if isinstance(reward, (int, float)):
        return float(reward)
    return 0.0
