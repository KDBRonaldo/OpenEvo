from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Generic, Iterable, TypeVar


TrialT = TypeVar("TrialT")


@dataclass(frozen=True)
class CandidateEvaluation(Generic[TrialT]):
    candidate_id: str
    source_index: int
    candidate_index: int
    generation: int
    objective: float | None
    trials: tuple[TrialT, ...]


@dataclass(frozen=True)
class RoundState(Generic[TrialT]):
    generation_inputs: tuple[TrialT, ...]
    dataset_history: tuple[str, ...]
    round_dataset_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoundTransition(Generic[TrialT]):
    winner: CandidateEvaluation[TrialT]
    next_round_inputs: tuple[TrialT, ...]
    dataset_history: tuple[str, ...]
    round_dataset_ids: tuple[str, ...]


def begin_round(
    *,
    generation_inputs: Iterable[TrialT],
    dataset_history: Iterable[str],
) -> RoundState[TrialT]:
    return RoundState(
        generation_inputs=tuple(generation_inputs),
        dataset_history=tuple(dataset_history),
        round_dataset_ids=(),
    )


def record_dataset(
    state: RoundState[TrialT],
    dataset_artifact_id: str | None,
) -> RoundState[TrialT]:
    if dataset_artifact_id is None:
        return state
    return RoundState(
        generation_inputs=state.generation_inputs,
        dataset_history=(*state.dataset_history, dataset_artifact_id),
        round_dataset_ids=(*state.round_dataset_ids, dataset_artifact_id),
    )


def resolve_candidate_index(
    *,
    candidate_id: str,
    explicit_candidate_index: int | None,
    fallback_candidate_index: int,
) -> int:
    if explicit_candidate_index is not None:
        return explicit_candidate_index
    match = re.search(r"(?:^|[-_])c?(\d+)(?:$|[-_])", candidate_id)
    if match:
        return int(match.group(1))
    return fallback_candidate_index


def per_task_candidate(
    *,
    candidate_id: str,
    source_index: int,
    explicit_candidate_index: int | None,
    fallback_candidate_index: int,
    generation: int | None,
    reward: float | None,
    trial: TrialT,
) -> CandidateEvaluation[TrialT]:
    return CandidateEvaluation(
        candidate_id=candidate_id,
        source_index=source_index,
        candidate_index=resolve_candidate_index(
            candidate_id=candidate_id,
            explicit_candidate_index=explicit_candidate_index,
            fallback_candidate_index=fallback_candidate_index,
        ),
        generation=0 if generation is None else generation,
        objective=reward,
        trials=(trial,),
    )


def macro_mean(rewards: Iterable[float | None]) -> float | None:
    values = tuple(rewards)
    if not values:
        raise ValueError("group score requires at least one task reward")
    if any(reward is None for reward in values):
        return None
    return sum(float(reward) for reward in values) / len(values)


def group_candidate(
    *,
    candidate_id: str,
    source_index: int,
    explicit_candidate_index: int | None,
    fallback_candidate_index: int,
    generation: int | None,
    task_rewards: Iterable[float | None],
    task_trials: Iterable[TrialT],
) -> CandidateEvaluation[TrialT]:
    return CandidateEvaluation(
        candidate_id=candidate_id,
        source_index=source_index,
        candidate_index=resolve_candidate_index(
            candidate_id=candidate_id,
            explicit_candidate_index=explicit_candidate_index,
            fallback_candidate_index=fallback_candidate_index,
        ),
        generation=0 if generation is None else generation,
        objective=macro_mean(task_rewards),
        trials=tuple(task_trials),
    )


def advance_generation(
    state: RoundState[TrialT],
    candidates: Iterable[CandidateEvaluation[TrialT]],
) -> RoundState[TrialT]:
    return RoundState(
        generation_inputs=tuple(
            trial for candidate in candidates for trial in candidate.trials
        ),
        dataset_history=state.dataset_history,
        round_dataset_ids=state.round_dataset_ids,
    )


def select_round_winner(
    candidates: Iterable[CandidateEvaluation[TrialT]],
    *,
    empty_error: str = "no candidate trials were evaluated",
) -> CandidateEvaluation[TrialT]:
    candidate_list = list(candidates)
    if not candidate_list:
        raise ValueError(empty_error)
    return max(
        candidate_list,
        key=lambda candidate: (
            float("-inf") if candidate.objective is None else float(candidate.objective),
            candidate.generation,
            -candidate.candidate_index,
        ),
    )


def complete_round(
    state: RoundState[TrialT],
    candidates: Iterable[CandidateEvaluation[TrialT]],
    *,
    empty_error: str = "no candidate trials were evaluated",
) -> RoundTransition[TrialT]:
    winner = select_round_winner(candidates, empty_error=empty_error)
    return RoundTransition(
        winner=winner,
        next_round_inputs=winner.trials,
        dataset_history=state.dataset_history,
        round_dataset_ids=state.round_dataset_ids,
    )
