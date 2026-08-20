from __future__ import annotations

import httpx
import pytest

from openevo import experiments

RolloutHttpClient = experiments.RolloutHttpClient
EvolutionHttpClient = experiments.EvolutionHttpClient
EvolutionHttpStatusError = experiments.EvolutionHttpStatusError


def test_rollout_http_client_url_encodes_task_id_path_segment() -> None:
    captured_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.raw_path)
        return httpx.Response(
            200,
            json={"task_id": "bench?x#frag", "status": "completed"},
        )

    client = RolloutHttpClient(
        "http://rollout.example",
        transport=httpx.MockTransport(handler),
    )

    result = client.get_task("bench?x#frag")

    assert result["status"] == "completed"
    assert captured_paths == [b"/rollout/task/bench%3Fx%23frag"]


def test_rollout_http_client_cancel_requires_exact_terminal_authority() -> None:
    captured: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.raw_path))
        return httpx.Response(
            200,
            json={"task_id": "task?cancel#exact", "status": "cancelled"},
        )

    client = RolloutHttpClient(
        "http://rollout.example",
        transport=httpx.MockTransport(handler),
    )

    assert client.cancel_task("task?cancel#exact") == {
        "task_id": "task?cancel#exact",
        "status": "cancelled",
    }
    assert captured == [("DELETE", b"/rollout/task/task%3Fcancel%23exact")]


def test_rollout_http_client_rejects_non_object_submit_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    client = RolloutHttpClient(
        "http://rollout.example",
        transport=httpx.MockTransport(handler),
    )

    try:
        client.submit_task({"task_id": "task-a"})
    except ValueError as exc:
        assert "rollout submit response was not a JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_internal_clients_attach_generation_bound_headers_to_every_request() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        if request.url.path == "/rollout/task/task-a":
            return httpx.Response(200, json={"task_id": "task-a", "status": "completed"})
        return httpx.Response(200, json={"artifact_id": "artifact-a"})

    headers = {
        "Authorization": "Bearer private-generation-credential",
        "X-OpenEvo-Internal-Generation": "a" * 64,
        "X-OpenEvo-Internal-Registry": "b" * 64,
        "X-OpenEvo-Internal-Service": "core-control",
    }
    transport = httpx.MockTransport(handler)
    rollout = RolloutHttpClient(
        "http://127.0.0.1:18100",
        headers=headers,
        transport=transport,
    )
    evolution = EvolutionHttpClient(
        "http://127.0.0.1:18200",
        headers=headers,
        transport=transport,
    )

    rollout.get_task("task-a")
    evolution.get_artifact("artifact-a")

    assert len(captured) == 2
    assert all(
        item["authorization"] == "Bearer private-generation-credential"
        and item["x-openevo-internal-service"] == "core-control"
        for item in captured
    )


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (409, False),
        (422, False),
        (429, True),
        (503, True),
    ],
)
def test_evolution_http_client_exposes_closed_retryability(
    status_code: int,
    retryable: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "remote failure"})

    client = EvolutionHttpClient(
        "http://evolution.example",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.create_dataset({"name": "dataset"})

    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable
    assert captured.value.detail == "remote failure"
    assert str(captured.value).endswith(": remote failure")


def test_evolution_http_client_bounds_remote_error_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad\n" + ("x" * 700)})

    client = EvolutionHttpClient(
        "http://evolution.example",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.create_dataset({"name": "dataset"})

    assert captured.value.detail is not None
    assert "\n" not in captured.value.detail
    assert len(captured.value.detail.encode("utf-8")) <= 512
