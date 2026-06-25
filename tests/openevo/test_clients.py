from __future__ import annotations

import httpx

from openevo.experiment.clients import RolloutHttpClient


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
