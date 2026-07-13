from __future__ import annotations

import ast
import inspect

import pytest

import openevo.evolution.agent_system_gepa_kernel as gepa_kernel


def _state_event(state: gepa_kernel.RoundState[str]) -> dict[str, object]:
    return {
        "generation_inputs": list(state.generation_inputs),
        "dataset_history": list(state.dataset_history),
        "round_dataset_ids": list(state.round_dataset_ids),
    }


def _transition_event(
    transition: gepa_kernel.RoundTransition[str],
) -> dict[str, object]:
    return {
        "winner": transition.winner.candidate_id,
        "winner_generation": transition.winner.generation,
        "winner_candidate_index": transition.winner.candidate_index,
        "winner_objective": transition.winner.objective,
        "next_round_inputs": list(transition.next_round_inputs),
        "dataset_history": list(transition.dataset_history),
        "round_dataset_ids": list(transition.round_dataset_ids),
    }


def test_per_task_event_tape_preserves_gepa_state_and_selection() -> None:
    next_round_inputs = ("baseline",)
    dataset_history: tuple[str, ...] = ()
    tape: list[dict[str, object]] = []

    round_specs = [
        [
            [("r1-g1-c2", 2, None), ("r1-g1-c1", 1, 0.4)],
            [("r1-g2-c2", 2, 0.4), ("r1-g2-c1", 1, 0.4)],
        ],
        [
            [("r2-g1-c1", 1, 0.5), ("r2-g1-c2", 2, 0.6)],
            [("r2-g2-c2", 2, 0.6), ("r2-g2-c1", 1, 0.6)],
        ],
    ]

    for round_number, generation_specs in enumerate(round_specs, start=1):
        state = gepa_kernel.begin_round(
            generation_inputs=next_round_inputs,
            dataset_history=dataset_history,
        )
        round_candidates: list[gepa_kernel.CandidateEvaluation[str]] = []
        tape.append({"round": round_number, "start": _state_event(state)})

        for generation, candidate_specs in enumerate(generation_specs, start=1):
            generation_inputs = list(state.generation_inputs)
            state = gepa_kernel.record_dataset(state, f"dataset-r{round_number}-g{generation}")
            candidates = [
                gepa_kernel.per_task_candidate(
                    candidate_id=payload,
                    source_index=len(round_candidates) + position - 1,
                    explicit_candidate_index=candidate_index,
                    fallback_candidate_index=position,
                    generation=generation,
                    reward=reward,
                    trial=payload,
                )
                for position, (payload, candidate_index, reward) in enumerate(
                    candidate_specs,
                    start=1,
                )
            ]
            round_candidates.extend(candidates)
            state = gepa_kernel.advance_generation(state, candidates)
            tape.append(
                {
                    "round": round_number,
                    "generation": generation,
                    "inputs": generation_inputs,
                    "objectives": [candidate.objective for candidate in candidates],
                    "after": _state_event(state),
                }
            )

        transition = gepa_kernel.complete_round(state, round_candidates)
        tape.append({"round": round_number, "summary": _transition_event(transition)})
        next_round_inputs = transition.next_round_inputs
        dataset_history = transition.dataset_history

    assert tape == [
        {
            "round": 1,
            "start": {
                "generation_inputs": ["baseline"],
                "dataset_history": [],
                "round_dataset_ids": [],
            },
        },
        {
            "round": 1,
            "generation": 1,
            "inputs": ["baseline"],
            "objectives": [None, 0.4],
            "after": {
                "generation_inputs": ["r1-g1-c2", "r1-g1-c1"],
                "dataset_history": ["dataset-r1-g1"],
                "round_dataset_ids": ["dataset-r1-g1"],
            },
        },
        {
            "round": 1,
            "generation": 2,
            "inputs": ["r1-g1-c2", "r1-g1-c1"],
            "objectives": [0.4, 0.4],
            "after": {
                "generation_inputs": ["r1-g2-c2", "r1-g2-c1"],
                "dataset_history": ["dataset-r1-g1", "dataset-r1-g2"],
                "round_dataset_ids": ["dataset-r1-g1", "dataset-r1-g2"],
            },
        },
        {
            "round": 1,
            "summary": {
                "winner": "r1-g2-c1",
                "winner_generation": 2,
                "winner_candidate_index": 1,
                "winner_objective": 0.4,
                "next_round_inputs": ["r1-g2-c1"],
                "dataset_history": ["dataset-r1-g1", "dataset-r1-g2"],
                "round_dataset_ids": ["dataset-r1-g1", "dataset-r1-g2"],
            },
        },
        {
            "round": 2,
            "start": {
                "generation_inputs": ["r1-g2-c1"],
                "dataset_history": ["dataset-r1-g1", "dataset-r1-g2"],
                "round_dataset_ids": [],
            },
        },
        {
            "round": 2,
            "generation": 1,
            "inputs": ["r1-g2-c1"],
            "objectives": [0.5, 0.6],
            "after": {
                "generation_inputs": ["r2-g1-c1", "r2-g1-c2"],
                "dataset_history": [
                    "dataset-r1-g1",
                    "dataset-r1-g2",
                    "dataset-r2-g1",
                ],
                "round_dataset_ids": ["dataset-r2-g1"],
            },
        },
        {
            "round": 2,
            "generation": 2,
            "inputs": ["r2-g1-c1", "r2-g1-c2"],
            "objectives": [0.6, 0.6],
            "after": {
                "generation_inputs": ["r2-g2-c2", "r2-g2-c1"],
                "dataset_history": [
                    "dataset-r1-g1",
                    "dataset-r1-g2",
                    "dataset-r2-g1",
                    "dataset-r2-g2",
                ],
                "round_dataset_ids": ["dataset-r2-g1", "dataset-r2-g2"],
            },
        },
        {
            "round": 2,
            "summary": {
                "winner": "r2-g2-c1",
                "winner_generation": 2,
                "winner_candidate_index": 1,
                "winner_objective": 0.6,
                "next_round_inputs": ["r2-g2-c1"],
                "dataset_history": [
                    "dataset-r1-g1",
                    "dataset-r1-g2",
                    "dataset-r2-g1",
                    "dataset-r2-g2",
                ],
                "round_dataset_ids": ["dataset-r2-g1", "dataset-r2-g2"],
            },
        },
    ]


def test_group_event_tape_preserves_task_order_macro_mean_and_selection() -> None:
    task_ids = ("task-a", "task-b")
    next_round_inputs = ("baseline-a", "baseline-b")
    dataset_history: tuple[str, ...] = ()
    tape: list[dict[str, object]] = []
    round_specs = [
        [
            [("r1-g1-c2", 2, (None, 1.0)), ("r1-g1-c1", 1, (0.2, 0.6))],
            [("r1-g2-c2", 2, (0.4, 0.4)), ("r1-g2-c1", 1, (0.4, 0.4))],
        ],
        [
            [("r2-g1-c1", 1, (0.5, 0.5)), ("r2-g1-c2", 2, (0.6, 0.6))],
            [("r2-g2-c2", 2, (0.6, 0.6)), ("r2-g2-c1", 1, (0.6, 0.6))],
        ],
    ]

    for round_number, generation_specs in enumerate(round_specs, start=1):
        state = gepa_kernel.begin_round(
            generation_inputs=next_round_inputs,
            dataset_history=dataset_history,
        )
        round_candidates: list[gepa_kernel.CandidateEvaluation[str]] = []
        tape.append({"round": round_number, "start": _state_event(state)})

        for generation, candidate_specs in enumerate(generation_specs, start=1):
            generation_inputs = list(state.generation_inputs)
            state = gepa_kernel.record_dataset(state, f"dataset-r{round_number}-g{generation}")
            candidates = []
            for position, (payload, candidate_index, rewards) in enumerate(
                candidate_specs,
                start=1,
            ):
                trials = tuple(f"{payload}-{task_id}" for task_id in task_ids)
                candidates.append(
                    gepa_kernel.group_candidate(
                        candidate_id=payload,
                        source_index=len(round_candidates) + position - 1,
                        explicit_candidate_index=candidate_index,
                        fallback_candidate_index=position,
                        generation=generation,
                        task_rewards=rewards,
                        task_trials=trials,
                    )
                )
            round_candidates.extend(candidates)
            state = gepa_kernel.advance_generation(state, candidates)
            tape.append(
                {
                    "round": round_number,
                    "generation": generation,
                    "inputs": generation_inputs,
                    "objectives": [candidate.objective for candidate in candidates],
                    "after": _state_event(state),
                }
            )

        transition = gepa_kernel.complete_round(state, round_candidates)
        tape.append({"round": round_number, "summary": _transition_event(transition)})
        next_round_inputs = transition.next_round_inputs
        dataset_history = transition.dataset_history

    assert tape == [
        {
            "round": 1,
            "start": {
                "generation_inputs": ["baseline-a", "baseline-b"],
                "dataset_history": [],
                "round_dataset_ids": [],
            },
        },
        {
            "round": 1,
            "generation": 1,
            "inputs": ["baseline-a", "baseline-b"],
            "objectives": [None, pytest.approx(0.4)],
            "after": {
                "generation_inputs": [
                    "r1-g1-c2-task-a",
                    "r1-g1-c2-task-b",
                    "r1-g1-c1-task-a",
                    "r1-g1-c1-task-b",
                ],
                "dataset_history": ["dataset-r1-g1"],
                "round_dataset_ids": ["dataset-r1-g1"],
            },
        },
        {
            "round": 1,
            "generation": 2,
            "inputs": [
                "r1-g1-c2-task-a",
                "r1-g1-c2-task-b",
                "r1-g1-c1-task-a",
                "r1-g1-c1-task-b",
            ],
            "objectives": [pytest.approx(0.4), pytest.approx(0.4)],
            "after": {
                "generation_inputs": [
                    "r1-g2-c2-task-a",
                    "r1-g2-c2-task-b",
                    "r1-g2-c1-task-a",
                    "r1-g2-c1-task-b",
                ],
                "dataset_history": ["dataset-r1-g1", "dataset-r1-g2"],
                "round_dataset_ids": ["dataset-r1-g1", "dataset-r1-g2"],
            },
        },
        {
            "round": 1,
            "summary": {
                "winner": "r1-g2-c1",
                "winner_generation": 2,
                "winner_candidate_index": 1,
                "winner_objective": pytest.approx(0.4),
                "next_round_inputs": ["r1-g2-c1-task-a", "r1-g2-c1-task-b"],
                "dataset_history": ["dataset-r1-g1", "dataset-r1-g2"],
                "round_dataset_ids": ["dataset-r1-g1", "dataset-r1-g2"],
            },
        },
        {
            "round": 2,
            "start": {
                "generation_inputs": ["r1-g2-c1-task-a", "r1-g2-c1-task-b"],
                "dataset_history": ["dataset-r1-g1", "dataset-r1-g2"],
                "round_dataset_ids": [],
            },
        },
        {
            "round": 2,
            "generation": 1,
            "inputs": ["r1-g2-c1-task-a", "r1-g2-c1-task-b"],
            "objectives": [pytest.approx(0.5), pytest.approx(0.6)],
            "after": {
                "generation_inputs": [
                    "r2-g1-c1-task-a",
                    "r2-g1-c1-task-b",
                    "r2-g1-c2-task-a",
                    "r2-g1-c2-task-b",
                ],
                "dataset_history": [
                    "dataset-r1-g1",
                    "dataset-r1-g2",
                    "dataset-r2-g1",
                ],
                "round_dataset_ids": ["dataset-r2-g1"],
            },
        },
        {
            "round": 2,
            "generation": 2,
            "inputs": [
                "r2-g1-c1-task-a",
                "r2-g1-c1-task-b",
                "r2-g1-c2-task-a",
                "r2-g1-c2-task-b",
            ],
            "objectives": [pytest.approx(0.6), pytest.approx(0.6)],
            "after": {
                "generation_inputs": [
                    "r2-g2-c2-task-a",
                    "r2-g2-c2-task-b",
                    "r2-g2-c1-task-a",
                    "r2-g2-c1-task-b",
                ],
                "dataset_history": [
                    "dataset-r1-g1",
                    "dataset-r1-g2",
                    "dataset-r2-g1",
                    "dataset-r2-g2",
                ],
                "round_dataset_ids": ["dataset-r2-g1", "dataset-r2-g2"],
            },
        },
        {
            "round": 2,
            "summary": {
                "winner": "r2-g2-c1",
                "winner_generation": 2,
                "winner_candidate_index": 1,
                "winner_objective": pytest.approx(0.6),
                "next_round_inputs": ["r2-g2-c1-task-a", "r2-g2-c1-task-b"],
                "dataset_history": [
                    "dataset-r1-g1",
                    "dataset-r1-g2",
                    "dataset-r2-g1",
                    "dataset-r2-g2",
                ],
                "round_dataset_ids": ["dataset-r2-g1", "dataset-r2-g2"],
            },
        },
    ]


def test_kernel_is_stdlib_only_and_has_no_benchmark_io_dependency() -> None:
    tree = ast.parse(inspect.getsource(gepa_kernel))
    imported_roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported_roots <= {"__future__", "dataclasses", "re", "typing"}

    forbidden_names = {"Path", "Harbor", "subprocess"}
    assert not {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }.intersection(forbidden_names)
    assert not any(
        "terminal_bench" in node.value.lower() or "harbor" in node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
def test_candidate_index_fallback_and_complete_ties_are_stable() -> None:
    assert gepa_kernel.resolve_candidate_index(
        candidate_id="artifact-c7-output",
        explicit_candidate_index=None,
        fallback_candidate_index=0,
    ) == 7
    assert gepa_kernel.resolve_candidate_index(
        candidate_id="artifact-unknown",
        explicit_candidate_index=None,
        fallback_candidate_index=0,
    ) == 0

    candidates = [
        gepa_kernel.per_task_candidate(
            candidate_id=payload,
            source_index=position - 1,
            explicit_candidate_index=2,
            fallback_candidate_index=position,
            generation=3,
            reward=0.5,
            trial=f"{payload}-trial",
        )
        for position, payload in enumerate(("first", "second"), start=1)
    ]
    assert gepa_kernel.complete_round(
        gepa_kernel.begin_round(generation_inputs=("baseline",), dataset_history=()),
        candidates,
    ).winner is candidates[0]
