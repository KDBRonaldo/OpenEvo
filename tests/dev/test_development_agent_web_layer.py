from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from desktop.sidecar.contracts.v2 import models as m
from openevo.backend.contracts.v2 import models as core_m
from openevo.web_gateway.product_app import (
    DevelopmentDaemonClient,
    DevelopmentDaemonEventCursorExpired,
    DevelopmentAgentDesktopV2Provider,
    DevelopmentAgentStateEventRelay,
    EVENT_SCHEMA_SHA256,
    OPENAPI_SHA256,
    create_development_agent_web_app,
)
from desktop.sidecar.event_broker_v2 import DesktopEventBrokerV2


class FakeDaemonClient:
    def __init__(self) -> None:
        self.state_requests = 0
        self.task_observation_requests = 0
        self.state = {
            "schema_version": "1",
            "active_project_id": None,
            "projects": [],
            "sessions": [],
            "artifacts": [],
            "evolution_jobs": [],
            "evolution_runs": [],
            "project_heads": [],
            "workspaces": [],
        }
        self.events: list[dict[str, object]] = []
        self.expire_next_event_cursor = False
        self.workspace_files: dict[str, bytes] = {}
        self.evolution_action_ids: dict[str, str] = {}
        self.evolution_retry_action_ids: dict[str, str] = {}
        self.models: list[dict[str, object]] = []

    def emit_event(self, project_id: str) -> None:
        sequence = len(self.events) + 1
        self.events.append(
            {
                "sequence": sequence,
                "event_id": f"development-event-{sequence}",
                "project_id": project_id,
                "event_type": "state_changed",
                "payload_sha256": f"{sequence:064x}",
                "occurred_at": "2026-08-23T00:00:00Z",
            }
        )

    def request(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        del method, body
        if path == "/state":
            self.state_requests += 1
            return self.state
        if path.startswith("/events?"):
            query = parse_qs(urlsplit(path).query)
            if "after" not in query:
                return {
                    "schema_version": "1",
                    "events": [],
                    "latest_sequence": len(self.events),
                    "has_more": False,
                }
            if self.expire_next_event_cursor:
                self.expire_next_event_cursor = False
                raise DevelopmentDaemonEventCursorExpired
            after = int(query["after"][0])
            if int(query.get("wait_ms", ["0"])[0]):
                time.sleep(0.01)
            events = [event for event in self.events if event["sequence"] > after]
            return {
                "schema_version": "1",
                "events": events[:100],
                "latest_sequence": len(self.events),
                "has_more": len(events) > 100,
            }
        if path == "/capabilities":
            return {
                "schema_version": "1",
                "authority": "development_catalog_unverified",
                "capabilities": {
                    "schema_version": "1",
                    "core_version": "development",
                    "registry_digest": "a" * 64,
                    "evaluated_profile": {
                        "execution_mode": "subscription",
                        "capture_mode": "transcript",
                        "harness_id": "codex",
                        "harness_capabilities": [],
                        "runtime_capabilities": [],
                    },
                    "targets": [],
                },
            }
        raise AssertionError(path)

    def request_v2(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        parsed = urlsplit(path)
        if parsed.path == "/development/state":
            self.state_requests += 1
            return {
                "schema_version": "2",
                "active_project_id": self.state["active_project_id"],
                "projects": [
                    {"schema_version": "2", **project}
                    for project in self.state["projects"]
                ],
                "project_heads": [
                    {"schema_version": "2", **head}
                    for head in self.state["project_heads"]
                ],
            }
        if parsed.path == "/development/capabilities":
            legacy = self.request("/capabilities")
            return {
                "schema_version": "2",
                "authority": legacy["authority"],
                "capabilities": legacy["capabilities"],
            }
        if parsed.path == "/development/events":
            legacy = self.request(f"/events?{parsed.query}")
            return {**legacy, "schema_version": "2"}
        if parsed.path == "/development/projects" and method == "POST":
            assert isinstance(body, dict)
            existing = next(
                (item for item in self.state["projects"] if item["project_id"] == body["project_id"]),
                None,
            )
            if existing is None:
                existing = {
                    "project_id": body["project_id"],
                    "display_name": body["display_name"],
                    "config": body["config"],
                    "created_at": "2026-08-23T00:00:00Z",
                    "updated_at": "2026-08-23T00:00:00Z",
                }
                self.state["projects"].append(existing)
            self.state["active_project_id"] = existing["project_id"]
            return {"schema_version": "2", **existing}
        activate_project = re.fullmatch(r"/development/projects/([^/]+)/activate", parsed.path)
        if activate_project and method == "POST":
            project = next(item for item in self.state["projects"] if item["project_id"] == activate_project.group(1))
            self.state["active_project_id"] = project["project_id"]
            return {"schema_version": "2", **project}
        update_project = re.fullmatch(r"/development/projects/([^/]+)", parsed.path)
        if update_project and method == "PUT":
            assert isinstance(body, dict)
            project = next(item for item in self.state["projects"] if item["project_id"] == update_project.group(1))
            project.update({
                "display_name": body["display_name"],
                "config": body["config"],
                "updated_at": "2026-08-23T00:00:01Z",
            })
            return {"schema_version": "2", **project}
        if update_project and method == "DELETE":
            assert isinstance(body, dict)
            project_id = update_project.group(1)
            self.state["projects"] = [
                item for item in self.state["projects"]
                if item["project_id"] != project_id
            ]
            if self.state["active_project_id"] == project_id:
                self.state["active_project_id"] = None
            return {
                "schema_version": "2",
                "action_id": body["action_id"],
                "resource_kind": "project",
                "resource_id": project_id,
                "active_project_id": self.state["active_project_id"],
            }
        if parsed.path == "/development/tasks" and method == "POST":
            assert isinstance(body, dict)
            session_id = f"dev-session-{hashlib.sha256(body['action_id'].encode()).hexdigest()[:16]}"
            existing = next(
                (item for item in self.state["sessions"] if item["session_id"] == session_id),
                None,
            )
            if existing is None:
                existing = {
                    "session_id": session_id,
                    "project_id": body["project_id"],
                    "project_head_id": body.get("project_head_id"),
                    "task_title": body["task_title"],
                    "instruction": body["instruction"],
                    "response": None,
                    "model": None,
                    "state": "running",
                    "duration_ms": None,
                    "logs": ["Remote development daemon admitted the session."],
                    "selected_evolution": [],
                    "evolution_errors": [],
                    "workspace_changes": [],
                    "context_artifact_ids": [],
                    "runtime_activation": None,
                    "evolution_evidence_ready": False,
                    "error": None,
                    "created_at": "2026-08-23T00:00:00Z",
                    "updated_at": "2026-08-23T00:00:00Z",
                }
                self.state["sessions"].append(existing)
            return self._task_presentation(existing)
        cancel_task = re.fullmatch(r"/development/tasks/([^/]+)/cancel", parsed.path)
        if cancel_task and method == "POST":
            session = next(
                item for item in self.state["sessions"]
                if item["session_id"] == cancel_task.group(1)
            )
            session["updated_at"] = "2026-08-23T00:00:02Z"
            return self._task_presentation(session)
        delete_task = re.fullmatch(r"/development/tasks/([^/]+)", parsed.path)
        if delete_task and method == "DELETE":
            assert isinstance(body, dict)
            task_id = delete_task.group(1)
            self.state["sessions"] = [
                item for item in self.state["sessions"]
                if item["session_id"] != task_id
            ]
            return {
                "schema_version": "2",
                "action_id": body["action_id"],
                "resource_kind": "task",
                "resource_id": task_id,
                "active_project_id": self.state["active_project_id"],
            }
        if parsed.path == "/tasks":
            self.task_observation_requests += 1
            items = []
            for session in self.state["sessions"]:
                state = "closed" if session["state"] == "completed" else session["state"]
                items.append({
                    "schema_version": "2",
                    "task_id": session["session_id"],
                    "project_id": session["project_id"],
                    "project_head_id": session.get("project_head_id"),
                    "state": state,
                    "created_at": session["created_at"],
                    "updated_at": session["updated_at"],
                })
            query = parse_qs(parsed.query)
            after = query.get("after", [None])[0]
            start = 0
            if after is not None:
                start = next(
                    index + 1
                    for index, item in enumerate(items)
                    if item["task_id"] == after
                )
            limit = int(query.get("limit", ["100"])[0])
            page_items = items[start : start + limit]
            has_more = start + len(page_items) < len(items)
            return {
                "schema_version": "2",
                "items": page_items,
                "next_cursor": page_items[-1]["task_id"] if has_more else None,
                "has_more": has_more,
            }
        task_logs = re.fullmatch(r"/tasks/([^/]+)/logs", parsed.path)
        if task_logs:
            session = next(
                item for item in self.state["sessions"]
                if item["session_id"] == task_logs.group(1)
            )
            messages = [
                *[("system", message) for message in session.get("logs", [])],
                *([("transcript", session["response"])] if session.get("response") else []),
                *([("system", session["error"])] if session.get("error") else []),
            ]
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            all_items = [{
                "sequence": index + 1,
                "occurred_at": session["updated_at"],
                "stream": stream,
                "message": message,
            } for index, (stream, message) in enumerate(messages)]
            remaining = [item for item in all_items if item["sequence"] > after]
            items = remaining[:limit]
            has_more = len(remaining) > limit
            return {
                "schema_version": "2",
                "items": items,
                "next_cursor": str(items[-1]["sequence"]) if has_more else None,
                "has_more": has_more,
            }
        task_timeline = re.fullmatch(r"/tasks/([^/]+)/timeline", parsed.path)
        if task_timeline:
            session = next(
                item for item in self.state["sessions"]
                if item["session_id"] == task_timeline.group(1)
            )
            events = [{
                "schema_version": "2",
                "event_id": f"{session['session_id']}-event-admitted",
                "sequence": 1,
                "occurred_at": session["created_at"],
                "project_id": session["project_id"],
                "task_id": session["session_id"],
                "event_type": "task_admitted",
            }, {
                "schema_version": "2",
                "event_id": f"{session['session_id']}-event-attempt",
                "sequence": 2,
                "occurred_at": session["created_at"],
                "project_id": session["project_id"],
                "task_id": session["session_id"],
                "event_type": "attempt_appended",
            }]
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            remaining = [event for event in events if event["sequence"] > after]
            items = remaining[:limit]
            has_more = len(remaining) > limit
            return {
                "schema_version": "2",
                "items": items,
                "next_cursor": str(items[-1]["sequence"]) if has_more else None,
                "has_more": has_more,
            }
        task_artifacts = re.fullmatch(r"/tasks/([^/]+)/artifacts", parsed.path)
        if task_artifacts:
            items = [
                self._core_artifact(artifact)
                for artifact in self.state["artifacts"]
                if artifact["session_id"] == task_artifacts.group(1)
            ]
            return {
                "schema_version": "2",
                "items": items,
                "next_cursor": None,
                "has_more": False,
            }
        artifact_content = re.fullmatch(r"/artifacts/([^/]+)/content", parsed.path)
        if artifact_content:
            raw = next(
                artifact for artifact in self.state["artifacts"]
                if artifact["artifact_id"] == artifact_content.group(1)
            )
            return {
                "schema_version": "2",
                "artifact": self._core_artifact(raw),
                "media_type": raw["documents"][0]["media_type"] if raw["documents"] else "application/octet-stream",
                "content_sha256": raw["content_sha256"],
                "byte_size": raw["byte_size"],
            }
        artifact_detail = re.fullmatch(r"/artifacts/([^/]+)", parsed.path)
        if artifact_detail:
            raw = next(
                artifact for artifact in self.state["artifacts"]
                if artifact["artifact_id"] == artifact_detail.group(1)
            )
            return self._core_artifact(raw)
        raise AssertionError(path)

    @staticmethod
    def _task_presentation(session: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "2",
            "task_id": session["session_id"],
            "project_id": session["project_id"],
            "project_head_id": session.get("project_head_id"),
            "task_title": session["task_title"],
            "instruction": session["instruction"],
            "response": session.get("response"),
            "model": session.get("model"),
            "state": session["state"],
            "duration_ms": session.get("duration_ms"),
            "selected_evolution": [
                {"schema_version": "2", **item}
                for item in session.get("selected_evolution", [])
            ],
            "evolution_errors": [
                {"schema_version": "2", **item}
                for item in session.get("evolution_errors", [])
            ],
            "workspace_changes": [
                {
                    "schema_version": "2",
                    **item,
                    "diff_lines": [
                        {"schema_version": "2", **line}
                        for line in item.get("diff_lines", [])
                    ],
                }
                for item in session.get("workspace_changes", [])
            ],
            "context_artifact_ids": session.get("context_artifact_ids", []),
            "evolution_evidence_ready": session.get("evolution_evidence_ready", False),
            "error": session.get("error"),
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
        }

    @staticmethod
    def _core_artifact(raw: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "2",
            "artifact_id": raw["artifact_id"],
            "project_id": raw["project_id"],
            "artifact_type": "diagnostic" if raw["artifact_type"] == "report" else raw["artifact_type"],
            "manifest_sha256": raw["content_sha256"],
            "byte_size": raw["byte_size"],
            "created_at": raw["created_at"],
        }

    def proxy_v2(
        self,
        path: str,
        *,
        query: str,
        method: str,
        body: bytes,
        content_type: str | None,
        content_sha256: str | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        del content_type
        parameters = parse_qs(query)
        if path == "development/models" and method == "GET":
            return 200, json.dumps(
                {"schema_version": "2", "items": self.models}
            ).encode(), {"Content-Type": "application/json"}
        if path == "development/models" and method == "POST":
            registration = json.loads(body)
            model = {
                "schema_version": "2",
                "model_resource_id": "model-fixture",
                "repository_id": registration["repository_id"],
                "requested_revision": registration["revision"],
                "resolved_revision": None,
                "manifest_sha256": None,
                "state": "downloading",
                "downloaded_bytes": 0,
                "total_bytes": 128,
                "error": None,
                "created_at": "2026-09-02T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
            }
            self.models[:] = [model]
            return 202, json.dumps(model).encode(), {"Content-Type": "application/json"}
        model_detail = re.fullmatch(r"development/models/([^/]+)", path)
        if model_detail and method == "GET":
            model = next(
                item for item in self.models
                if item["model_resource_id"] == model_detail.group(1)
            )
            return 200, json.dumps(model).encode(), {"Content-Type": "application/json"}
        model_retry = re.fullmatch(r"development/models/([^/]+)/retry", path)
        if model_retry and method == "POST":
            model = next(
                item for item in self.models
                if item["model_resource_id"] == model_retry.group(1)
            )
            return 202, json.dumps(model).encode(), {"Content-Type": "application/json"}
        if path == "development/tasks" and method == "GET":
            project_id = parameters["project_id"][0]
            payload = {
                "schema_version": "2",
                "items": [
                    self._task_presentation(session)
                    for session in self.state["sessions"]
                    if session["project_id"] == project_id
                ],
                "next_cursor": None,
                "has_more": False,
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        delete_task = re.fullmatch(r"development/tasks/([^/]+)", path)
        if delete_task and method == "DELETE":
            deletion = json.loads(body)
            task_id = delete_task.group(1)
            self.state["sessions"] = [
                item for item in self.state["sessions"]
                if item["session_id"] != task_id
            ]
            payload = {
                "schema_version": "2",
                "action_id": deletion["action_id"],
                "resource_kind": "task",
                "resource_id": task_id,
                "active_project_id": self.state["active_project_id"],
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        delete_project = re.fullmatch(r"development/projects/([^/]+)", path)
        if delete_project and method == "DELETE":
            deletion = json.loads(body)
            project_id = delete_project.group(1)
            self.state["projects"] = [
                item for item in self.state["projects"]
                if item["project_id"] != project_id
            ]
            if self.state["active_project_id"] == project_id:
                self.state["active_project_id"] = None
            payload = {
                "schema_version": "2",
                "action_id": deletion["action_id"],
                "resource_kind": "project",
                "resource_id": project_id,
                "active_project_id": self.state["active_project_id"],
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path == "development/evolution-jobs" and method == "GET":
            project_id = parameters["project_id"][0]
            session_projects = {
                session["session_id"]: session["project_id"]
                for session in self.state["sessions"]
            }
            items = [
                self._evolution_job_v2(job, project_id=project_id)
                for job in self.state["evolution_jobs"]
                if session_projects[job["session_id"]] == project_id
            ]
            payload = {
                "schema_version": "2",
                "items": items,
                "next_cursor": None,
                "has_more": False,
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        evolution_job_retry = re.fullmatch(
            r"development/evolution-jobs/([^/]+)/retry", path
        )
        if evolution_job_retry and method == "POST":
            retry = json.loads(body)
            job = next(
                item for item in self.state["evolution_jobs"]
                if item["job_id"] == evolution_job_retry.group(1)
            )
            attempt_id = self.evolution_retry_action_ids.get(retry["action_id"])
            if attempt_id is None:
                attempt_id = f"{job['job_id']}-attempt-{len(job['attempts']) + 1}"
                self.evolution_retry_action_ids[retry["action_id"]] = attempt_id
                job["attempts"].append({
                    "attempt_id": attempt_id,
                    "job_id": job["job_id"],
                    "ordinal": len(job["attempts"]) + 1,
                    "state": "running",
                    "stage": "input_resolution",
                    "artifact_ids": [],
                    "error_code": None,
                    "error_message": None,
                    "logs": ["Retry admitted with the original fixed inputs."],
                    "created_at": "2026-08-23T00:00:01Z",
                    "started_at": "2026-08-23T00:00:01Z",
                    "completed_at": None,
                    "updated_at": "2026-08-23T00:00:01Z",
                })
                job["state"] = "running"
                job["error"] = None
            project_id = next(
                session["project_id"] for session in self.state["sessions"]
                if session["session_id"] == job["session_id"]
            )
            payload = self._evolution_job_v2(job, project_id=project_id)
            return 202, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        evolution_job_detail = re.fullmatch(r"development/evolution-jobs/([^/]+)", path)
        if evolution_job_detail and method == "GET":
            job = next(
                item for item in self.state["evolution_jobs"]
                if item["job_id"] == evolution_job_detail.group(1)
            )
            project_id = next(
                session["project_id"] for session in self.state["sessions"]
                if session["session_id"] == job["session_id"]
            )
            payload = self._evolution_job_v2(job, project_id=project_id)
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path == "development/evolution-runs" and method == "GET":
            project_id = parameters["project_id"][0]
            items = [
                self._evolution_run_v2(run)
                for run in self.state["evolution_runs"]
                if run["project_id"] == project_id
            ]
            payload = {
                "schema_version": "2",
                "items": items,
                "next_cursor": None,
                "has_more": False,
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path == "development/evolution-runs" and method == "POST":
            creation = json.loads(body)
            existing_run_id = self.evolution_action_ids.get(creation["action_id"])
            if existing_run_id is None:
                run_id = f"evolution-run-{len(self.state['evolution_runs']) + 1}"
                run = {
                    "run_id": run_id,
                    "project_id": creation["project_id"],
                    "source_session_ids": creation["source_task_ids"],
                    "selections": [
                        {
                            "target_id": selection["target_id"],
                            "method": selection["method"],
                            "config": selection["config"],
                        }
                        for selection in creation["selections"]
                    ],
                    "state": "running",
                    "artifact_ids": [],
                    "error": None,
                    "created_at": "2026-08-23T00:00:00Z",
                    "updated_at": "2026-08-23T00:00:00Z",
                }
                self.state["evolution_runs"].append(run)
                self.evolution_action_ids[creation["action_id"]] = run_id
            else:
                run = next(
                    item for item in self.state["evolution_runs"]
                    if item["run_id"] == existing_run_id
                )
            payload = self._evolution_run_v2(run)
            return 202, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        evolution_apply = re.fullmatch(r"development/evolution-runs/([^/]+)/apply", path)
        if evolution_apply and method == "POST":
            run = next(
                item for item in self.state["evolution_runs"]
                if item["run_id"] == evolution_apply.group(1)
            )
            run["state"] = "applied"
            run["updated_at"] = "2026-08-23T00:00:01Z"
            payload = self._evolution_run_v2(run)
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        evolution_detail = re.fullmatch(r"development/evolution-runs/([^/]+)", path)
        if evolution_detail and method == "GET":
            run = next(
                item for item in self.state["evolution_runs"]
                if item["run_id"] == evolution_detail.group(1)
            )
            payload = self._evolution_run_v2(run)
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path == "development/artifacts" and method == "GET":
            project_id = parameters["project_id"][0]
            items = [
                {
                    "schema_version": "2",
                    **artifact,
                    "documents": [
                        {"schema_version": "2", **document}
                        for document in artifact["documents"]
                    ],
                }
                for artifact in self.state["artifacts"]
                if artifact["project_id"] == project_id
            ]
            payload = {
                "schema_version": "2",
                "items": items,
                "next_cursor": None,
                "has_more": False,
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        artifact_detail = re.fullmatch(r"development/artifacts/([^/]+)", path)
        if artifact_detail and method == "GET":
            artifact = next(
                item for item in self.state["artifacts"]
                if item["artifact_id"] == artifact_detail.group(1)
            )
            payload = {
                "schema_version": "2",
                **artifact,
                "documents": [
                    {"schema_version": "2", **document}
                    for document in artifact["documents"]
                ],
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path.endswith("/workspace") and method == "GET":
            entries = [
                {
                    "schema_version": "2",
                    "path": name,
                    "kind": "file",
                    "byte_size": len(payload),
                    "content_sha256": hashlib.sha256(payload).hexdigest(),
                    "media_type": "text/plain",
                    "content": payload.decode(),
                    "modified_at": "2026-08-23T00:00:00Z",
                }
                for name, payload in sorted(self.workspace_files.items())
            ]
            authority = hashlib.sha256(
                json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            payload = {
                "schema_version": "2",
                "project_id": path.split("/")[1],
                "manifest_sha256": authority,
                "items": entries,
                "next_cursor": None,
                "has_more": False,
                "truncated": False,
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path.endswith("/workspace/files"):
            relative_path = parameters["path"][0]
            project_id = path.split("/")[1]
            if method == "PUT":
                assert content_sha256 == hashlib.sha256(body).hexdigest()
                self.workspace_files[relative_path] = body
                entry = {
                    "schema_version": "2",
                    "path": relative_path,
                    "kind": "file",
                    "byte_size": len(body),
                    "content_sha256": content_sha256,
                    "media_type": "text/plain",
                    "content": body.decode(),
                    "modified_at": "2026-08-23T00:00:00Z",
                }
                result = {
                    "schema_version": "2",
                    "project_id": project_id,
                    "manifest_sha256": "a" * 64,
                    "entry": entry,
                }
                return 201, json.dumps(result).encode(), {"Content-Type": "application/json"}
            if method == "GET":
                payload = self.workspace_files[relative_path]
                return 200, payload, {
                    "Content-Type": "text/plain",
                    "X-OpenEvo-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                }
            if method == "DELETE":
                del self.workspace_files[relative_path]
                result = {
                    "schema_version": "2",
                    "project_id": project_id,
                    "manifest_sha256": "b" * 64,
                    "deleted_path": relative_path,
                }
                return 200, json.dumps(result).encode(), {"Content-Type": "application/json"}
        raise AssertionError((method, path, query))

    def _evolution_run_v2(self, run: dict[str, object]) -> dict[str, object]:
        action_id = next(
            (
                action_id for action_id, run_id in self.evolution_action_ids.items()
                if run_id == run["run_id"]
            ),
            f"legacy-{run['run_id']}",
        )
        return {
            "schema_version": "2",
            "run_id": run["run_id"],
            "action_id": action_id,
            "project_id": run["project_id"],
            "source_task_ids": run["source_session_ids"],
            "selections": [
                {"schema_version": "2", **selection}
                for selection in run["selections"]
            ],
            "state": run["state"],
            "artifact_ids": run["artifact_ids"],
            "error": run["error"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
        }

    def _evolution_job_v2(
        self,
        job: dict[str, object],
        *,
        project_id: str,
    ) -> dict[str, object]:
        action_ids = {
            attempt_id: action_id
            for action_id, attempt_id in self.evolution_retry_action_ids.items()
        }
        return {
            "schema_version": "2",
            "job_id": job["job_id"],
            "project_id": project_id,
            "task_id": job["session_id"],
            "run_id": job.get("run_id"),
            "target_id": job["target_id"],
            "method_id": job["method_id"],
            "requested_method_id": job["requested_method_id"],
            "resolver_input_artifact_ids": job["resolver_input_artifact_ids"],
            "previous_artifact_id": job["previous_artifact_id"],
            "config": job["config"],
            "state": job["state"],
            "artifact_ids": job["artifact_ids"],
            "error": job["error"],
            "attempts": [
                {
                    "schema_version": "2",
                    "action_id": action_ids.get(attempt["attempt_id"]),
                    **attempt,
                }
                for attempt in job["attempts"]
            ],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }


def _config() -> dict[str, object]:
    return {
        "schema_version": "2",
        "task": {"title": "Test task", "objective": "Return a concise result."},
        "workspace": {"kind": "scratch", "display_name": "Scratch"},
        "execution": {
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "harness_id": "codex",
            "codex_model": "gpt-5.5",
            "reasoning_effort": "high",
            "token_limit": 4096,
            "task_network_allow_internet": False,
        },
        "evolution": {"targets": {}},
    }


def test_provider_exposes_only_honest_development_features() -> None:
    version = DevelopmentAgentDesktopV2Provider(
        FakeDaemonClient(), source_commit="a" * 40
    ).invoke("getDesktopContractVersionV2", {})

    assert version.build_channel == "development"
    assert version.openapi_sha256 == OPENAPI_SHA256
    assert version.event_schema_sha256 == EVENT_SCHEMA_SHA256
    assert version.feature_flags == [
        "development_agent_bridge_v2",
        "event_replay_v2",
        "huggingface_model_management_v2",
        "mutation_idempotency_v2",
    ]
    assert "daemon_bundle_v2" not in version.feature_flags


def _event_from_sse(frame: bytes) -> dict[str, object]:
    data_line = next(
        line for line in frame.decode("utf-8").splitlines() if line.startswith("data: ")
    )
    return json.loads(data_line.removeprefix("data: "))


def test_state_event_relay_publishes_changes_and_replays_after_disconnect() -> None:
    fake = FakeDaemonClient()
    fake.state["active_project_id"] = "project-1"
    broker = DesktopEventBrokerV2(
        max_events=8,
        max_subscriber_events=8,
        heartbeat_interval=1,
        poll_interval=0.001,
        event_id_factory=iter(("event-one", "event-two")).__next__,
    )
    provider = DevelopmentAgentDesktopV2Provider(
        fake,
        source_commit="a" * 40,
        event_broker=broker,
    )
    relay = DevelopmentAgentStateEventRelay(
        client=fake,
        provider=provider,
        broker=broker,
    )

    assert relay.poll_once() is False
    first_subscription = broker.subscribe()
    fake.state["sessions"] = [{"session_id": "session-1", "status": "running"}]
    fake.emit_event("project-1")
    assert relay.poll_once(wait_milliseconds=0) is True
    first_event = _event_from_sse(asyncio.run(anext(first_subscription)))
    asyncio.run(first_subscription.aclose())

    fake.state["sessions"] = [{"session_id": "session-1", "status": "closed"}]
    fake.emit_event("project-1")
    assert relay.poll_once(wait_milliseconds=0) is True
    replay_subscription = broker.subscribe(str(first_event["event_id"]))
    replayed_event = _event_from_sse(asyncio.run(anext(replay_subscription)))
    asyncio.run(replay_subscription.aclose())

    assert first_event["event_type"] == "core_authority_changed"
    assert first_event["payload"]["core_event_sequence"] == 1
    assert replayed_event["event_type"] == "core_authority_changed"
    assert replayed_event["payload"]["core_event_sequence"] == 2
    assert replayed_event["event_id"] != first_event["event_id"]
    assert relay.poll_once(wait_milliseconds=0) is False
    broker.close()


def test_state_event_relay_resynchronizes_from_daemon_after_cursor_expiry() -> None:
    fake = FakeDaemonClient()
    fake.state["active_project_id"] = "project-1"
    broker = DesktopEventBrokerV2(
        max_events=8,
        max_subscriber_events=8,
        heartbeat_interval=1,
        poll_interval=0.001,
        event_id_factory=lambda: "resync-event",
    )
    provider = DevelopmentAgentDesktopV2Provider(
        fake,
        source_commit="a" * 40,
        event_broker=broker,
    )
    relay = DevelopmentAgentStateEventRelay(
        client=fake,
        provider=provider,
        broker=broker,
    )

    assert relay.poll_once(wait_milliseconds=0) is False
    fake.state["sessions"] = [{"session_id": "session-1", "status": "closed"}]
    fake.emit_event("project-1")
    fake.expire_next_event_cursor = True
    subscription = broker.subscribe()
    assert relay.poll_once(wait_milliseconds=0) is True
    event = _event_from_sse(asyncio.run(anext(subscription)))
    asyncio.run(subscription.aclose())

    assert event["payload"]["core_event_id"].startswith("development-resync-")
    assert event["payload"]["core_event_sequence"] == 1
    broker.close()


def test_daemon_client_accepts_bounded_aggregate_state_larger_than_one_mib(
    monkeypatch,
) -> None:
    payload = json.dumps({"schema_version": "1", "padding": "x" * 1_100_000}).encode()

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            assert limit > len(payload)
            return payload

    monkeypatch.setattr(
        "openevo.web_gateway.product_app.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )

    result = DevelopmentDaemonClient("http://127.0.0.1:8765", "secret").request("/state")

    assert result["schema_version"] == "1"


def test_daemon_client_forwards_delete_request_body(monkeypatch) -> None:
    captured: list[urllib.request.Request] = []
    response_payload = json.dumps({"schema_version": "2"}).encode()

    class Response:
        status = 200
        headers = {"Content-Length": str(len(response_payload))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            assert limit > len(response_payload)
            return response_payload

        def close(self) -> None:
            return None

    def open_request(request: urllib.request.Request, timeout: int) -> Response:
        assert timeout == 65
        captured.append(request)
        return Response()

    monkeypatch.setattr(
        "openevo.web_gateway.product_app.urllib.request.urlopen",
        open_request,
    )
    body = b'{"schema_version":"2","action_id":"delete-action"}'

    status, payload, _headers = DevelopmentDaemonClient(
        "http://127.0.0.1:8765", "secret"
    ).proxy_v2(
        "development/tasks/task-delete",
        query="",
        method="DELETE",
        body=body,
        content_type="application/json",
    )

    assert status == 200
    assert payload == response_payload
    assert captured[0].method == "DELETE"
    assert captured[0].data == body


def test_module_imports_when_launcher_is_started_from_desktop() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    scripts_root = repository_root / "scripts" / "dev"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(scripts_root)!r}); "
                "import development_agent_web_layer"
            ),
        ],
        cwd=repository_root / "desktop",
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

    compatibility_script = scripts_root / "development_agent_web_layer.py"
    assert len(compatibility_script.read_text(encoding="utf-8").splitlines()) < 20


def test_http_layer_requires_exact_session_and_projects_empty_state() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8765",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:5173",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    with TestClient(app) as client:
        assert client.get("/desktop/v2/projects").status_code == 401
        response = client.get(
            "/desktop/v2/projects",
            headers={"X-OpenEvo-Desktop-Session": "desktop-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "schema_version": "2",
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }
        missing = client.get(
            "/desktop/v2/projects/missing",
            headers={"X-OpenEvo-Desktop-Session": "desktop-secret"},
        )
        assert missing.status_code == 404
        assert missing.json() == {
            "schema_version": "2",
            "code": "desktop_resource_not_found",
            "summary": "project not found",
            "retryable": False,
            "action": "none",
            "affected_resource_id": None,
        }


def test_http_layer_proxies_authenticated_workspace_v2_with_verified_bytes() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    fake.state.update({
        "active_project_id": "project-workspace-v2",
        "projects": [{
            "project_id": "project-workspace-v2",
            "display_name": "Workspace v2",
            "config": _config(),
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
    })
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    root = "/desktop/v2/development/projects/project-workspace-v2/workspace"
    with TestClient(app) as client:
        assert client.get(root).status_code == 401
        initial = client.get(f"{root}?limit=100", headers=headers)
        assert initial.status_code == 200
        assert initial.json()["items"] == []

        created = client.put(
            f"{root}/files?path=notes%2Fanswer.txt&overwrite=false",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"OpenEvo v2\n",
        )
        assert created.status_code == 201
        assert created.json()["entry"]["content_sha256"] == hashlib.sha256(
            b"OpenEvo v2\n"
        ).hexdigest()

        inventory = client.get(f"{root}?limit=100", headers=headers)
        assert [entry["path"] for entry in inventory.json()["items"]] == [
            "notes/answer.txt"
        ]
        downloaded = client.get(
            f"{root}/files?path=notes%2Fanswer.txt", headers=headers
        )
        assert downloaded.content == b"OpenEvo v2\n"
        assert downloaded.headers["X-OpenEvo-Content-SHA256"] == hashlib.sha256(
            downloaded.content
        ).hexdigest()

        deleted = client.delete(
            f"{root}/files?path=notes%2Fanswer.txt", headers=headers
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted_path"] == "notes/answer.txt"


def test_http_layer_proxies_server_owned_model_downloads() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    root = "/desktop/v2/development/models"
    with TestClient(app) as client:
        assert client.get(root).status_code == 401
        registered = client.post(
            root,
            headers=headers,
            json={
                "schema_version": "2",
                "action_id": "register-model",
                "repository_id": "OpenEvo/Fixture-0.1B",
                "revision": "main",
            },
        )
        assert registered.status_code == 202
        assert registered.json()["repository_id"] == "OpenEvo/Fixture-0.1B"
        inventory = client.get(root, headers=headers)
        assert inventory.status_code == 200
        assert inventory.json()["items"] == [registered.json()]
        detail = client.get(f"{root}/model-fixture", headers=headers)
        assert detail.json() == registered.json()
        retry = client.post(
            f"{root}/model-fixture/retry",
            headers=headers,
            json={"schema_version": "2", "action_id": "retry-model"},
        )
        assert retry.status_code == 202
        assert retry.json()["model_resource_id"] == "model-fixture"


def test_http_layer_proxies_authenticated_project_and_task_deletions() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    project_id = "project-delete-v2"
    task_id = "task-delete-v2"
    fake.state.update({
        "active_project_id": project_id,
        "projects": [{
            "project_id": project_id,
            "display_name": "Delete v2",
            "config": _config(),
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
        "sessions": [{"session_id": task_id, "project_id": project_id}],
    })
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="d" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    with TestClient(app) as client:
        assert client.request(
            "DELETE",
            f"/desktop/v2/development/tasks/{task_id}",
            json={"schema_version": "2", "action_id": "delete-task-action"},
        ).status_code == 401
        task = client.request(
            "DELETE",
            f"/desktop/v2/development/tasks/{task_id}",
            headers=headers,
            json={"schema_version": "2", "action_id": "delete-task-action"},
        )
        assert task.status_code == 200
        assert task.json() == {
            "schema_version": "2",
            "action_id": "delete-task-action",
            "resource_kind": "task",
            "resource_id": task_id,
            "active_project_id": project_id,
        }

        project = client.request(
            "DELETE",
            f"/desktop/v2/development/projects/{project_id}",
            headers=headers,
            json={"schema_version": "2", "action_id": "delete-project-action"},
        )
        assert project.status_code == 200
        assert project.json() == {
            "schema_version": "2",
            "action_id": "delete-project-action",
            "resource_kind": "project",
            "resource_id": project_id,
            "active_project_id": None,
        }
        repeated = client.request(
            "DELETE",
            f"/desktop/v2/development/projects/{project_id}",
            headers=headers,
            json={"schema_version": "2", "action_id": "delete-project-action"},
        )
        assert repeated.status_code == 200
        assert repeated.json() == project.json()


def test_http_layer_uses_authenticated_daemon_v2_artifact_authority() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    project_id = "project-artifact-v2"
    task_id = "task-artifact-v2"
    content = "# Evolved skill\n"
    fake.state.update({
        "active_project_id": project_id,
        "projects": [{
            "project_id": project_id,
            "display_name": "Artifact v2",
            "config": _config(),
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
        "artifacts": [{
            "artifact_id": "artifact-skill-v2",
            "project_id": project_id,
            "session_id": task_id,
            "run_id": None,
            "target_id": "skill_bundle",
            "artifact_type": "skill_bundle",
            "method": "skill_bundle_reflector",
            "renderer_kind": "file_bundle",
            "documents": [{
                "path": "SKILL.md",
                "media_type": "text/markdown",
                "content": content,
            }],
            "manifest": {"content_path": "SKILL.md"},
            "content_path": "SKILL.md",
            "content": content,
            "content_sha256": hashlib.sha256(
                json.dumps(
                    [{"path": "SKILL.md", "media_type": "text/markdown", "content": content}],
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "byte_size": len(content.encode()),
            "previous_artifact_id": None,
            "promoted": False,
            "created_at": "2026-08-23T00:00:01Z",
        }],
    })
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    inventory_path = f"/desktop/v2/development/artifacts?project_id={project_id}&limit=5"
    with TestClient(app) as client:
        assert client.get(inventory_path).status_code == 401
        inventory = client.get(inventory_path, headers=headers)
        assert inventory.status_code == 200
        assert inventory.json()["items"][0]["documents"][0]["content"] == content

        detail = client.get(
            "/desktop/v2/development/artifacts/artifact-skill-v2",
            headers=headers,
        )
        assert detail.status_code == 200
        assert detail.json()["content_path"] == "SKILL.md"

        standard = client.get(
            "/desktop/v2/artifacts/artifact-skill-v2",
            headers=headers,
        )
        assert standard.status_code == 200
        assert standard.json()["artifact_type"] == "skill_bundle"
        content_metadata = client.get(
            "/desktop/v2/artifacts/artifact-skill-v2/content",
            headers=headers,
        )
        assert content_metadata.status_code == 200
        assert content_metadata.json()["media_type"] == "text/markdown"


def test_http_layer_uses_authenticated_daemon_v2_evolution_run_authority() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    project_id = "project-evolution-v2"
    fake.state.update({
        "active_project_id": project_id,
        "projects": [{
            "project_id": project_id,
            "display_name": "Evolution v2",
            "config": _config(),
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
    })
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    creation = {
        "schema_version": "2",
        "action_id": "action-evolution-v2",
        "project_id": project_id,
        "source_task_ids": ["task-evolution-v2"],
        "selections": [{
            "schema_version": "2",
            "target_id": "text_memory",
            "method": "text_memory_reflector",
            "config": {},
        }],
    }
    root = "/desktop/v2/development/evolution-runs"
    with TestClient(app) as client:
        assert client.get(f"{root}?project_id={project_id}").status_code == 401
        created = client.post(root, headers=headers, json=creation)
        assert created.status_code == 202
        assert created.json()["action_id"] == creation["action_id"]
        run_id = created.json()["run_id"]

        inventory = client.get(
            f"{root}?project_id={project_id}&limit=25", headers=headers
        )
        assert inventory.status_code == 200
        assert inventory.json()["items"][0]["run_id"] == run_id

        fake.state["evolution_runs"][0]["state"] = "candidate_ready"
        fake.state["evolution_runs"][0]["artifact_ids"] = ["candidate-memory-v2"]
        applied = client.post(
            f"{root}/{run_id}/apply",
            headers=headers,
            json={"schema_version": "2"},
        )
        assert applied.status_code == 200
        assert applied.json()["state"] == "applied"

        detail = client.get(f"{root}/{run_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["artifact_ids"] == ["candidate-memory-v2"]


def test_http_layer_uses_authenticated_daemon_v2_evolution_job_authority() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    project_id = "development-project-job-v2"
    task_id = "development-task-job-v2"
    job_id = "development-job-v2"
    fake.state.update({
        "active_project_id": project_id,
        "projects": [{
            "project_id": project_id,
            "display_name": "Evolution Job v2",
            "config": _config(),
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
        "sessions": [{
            "session_id": task_id,
            "project_id": project_id,
            "task_title": "Retry failed method",
            "instruction": "Produce reusable context.",
            "response": "Captured evidence.",
            "model": "test",
            "state": "completed",
            "duration_ms": 1,
            "logs": [],
            "selected_evolution": [],
            "evolution_errors": [],
            "workspace_changes": [],
            "context_artifact_ids": [],
            "runtime_activation": None,
            "error": None,
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
        "evolution_jobs": [{
            "job_id": job_id,
            "session_id": task_id,
            "run_id": None,
            "target_id": "text_memory",
            "method_id": "text_memory_reflector",
            "requested_method_id": "text_memory_reflector",
            "resolver_input_artifact_ids": [],
            "previous_artifact_id": None,
            "config": {},
            "state": "failed",
            "artifact_ids": [],
            "error": "temporary failure",
            "attempts": [{
                "attempt_id": f"{job_id}-attempt-1",
                "job_id": job_id,
                "ordinal": 1,
                "state": "failed",
                "stage": "method_execution",
                "artifact_ids": [],
                "error_code": "method_execution_failed",
                "error_message": "temporary failure",
                "logs": ["Evolution attempt failed."],
                "created_at": "2026-08-23T00:00:00Z",
                "started_at": "2026-08-23T00:00:00Z",
                "completed_at": "2026-08-23T00:00:00Z",
                "updated_at": "2026-08-23T00:00:00Z",
            }],
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
    })
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    root = "/desktop/v2/development/evolution-jobs"
    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    action_id = "retry-development-job-v2"
    with TestClient(app) as client:
        assert client.get(f"{root}?project_id={project_id}").status_code == 401
        inventory = client.get(
            f"{root}?project_id={project_id}&limit=25",
            headers=headers,
        )
        assert inventory.status_code == 200
        assert inventory.json()["items"][0]["job_id"] == job_id

        retried = client.post(
            f"{root}/{job_id}/retry",
            headers=headers,
            json={"schema_version": "2", "action_id": action_id},
        )
        assert retried.status_code == 202
        assert retried.json()["attempts"][-1]["action_id"] == action_id

        duplicate = client.post(
            f"{root}/{job_id}/retry",
            headers=headers,
            json={"schema_version": "2", "action_id": action_id},
        )
        assert duplicate.status_code == 202
        assert len(duplicate.json()["attempts"]) == 2

        detail = client.get(f"{root}/{job_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["project_id"] == project_id


def test_self_hosted_layer_serves_the_preserved_webui_renderer() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
            static_root=Path(__file__).resolve().parents[2]
            / "src"
            / "openevo"
            / "web_gateway"
            / "static",
        )
    finally:
        web.DevelopmentDaemonClient = original

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/openevo")
        bootstrap = client.post(
            "/openevo-native/browser/bootstrap",
            headers={"Origin": "http://127.0.0.1:8765"},
            json={"schema_version": "2", "bootstrap_token": "c" * 64},
        )
        asset_paths = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
        asset_statuses = [client.get(path).status_code for path in asset_paths]

    assert page.status_code == 200
    assert "<title>EvoLab</title>" in page.text
    assert asset_paths
    assert asset_statuses == [200] * len(asset_paths)
    assert bootstrap.status_code == 200
    assert bootstrap.json()["endpoint"] == "http://127.0.0.1:8765"
    assert bootstrap.json()["session_token"] == "desktop-secret"


def test_provider_projects_persisted_project_and_task_into_closed_v2_models() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [
                {
                    "project_id": "project-1",
                    "display_name": "Development project",
                    "config": _config(),
                    "created_at": "2026-08-22T00:00:00Z",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
            ],
            "sessions": [
                {
                    "session_id": "session-1",
                    "project_id": "project-1",
                    "state": "running",
                    "logs": ["started"],
                    "created_at": "2026-08-22T00:01:00Z",
                    "updated_at": "2026-08-22T00:01:00Z",
                }
            ],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    projects = provider.invoke("listDesktopProjectsV2", {})
    assert fake.task_observation_requests == 1
    fake.task_observation_requests = 0
    tasks = provider.invoke("listDesktopTasksV2", {"project_id": "project-1"})

    assert projects.items[0].active_project_head.project_id == "project-1"
    assert tasks.items[0].task_id == "session-1"
    assert tasks.items[0].state == "running"
    assert tasks.items[0].admission.predecessor_project_head.project_id == "project-1"
    assert fake.task_observation_requests == 0


def test_provider_pages_more_than_one_hundred_tasks_without_capping_history() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [
                {
                    "project_id": "project-1",
                    "display_name": "Development project",
                    "config": _config(),
                    "created_at": "2026-08-22T00:00:00Z",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
            ],
            "sessions": [
                {
                    "session_id": f"session-{index:03d}",
                    "project_id": "project-1",
                    "state": "running",
                    "logs": ["started"],
                    "created_at": "2026-08-22T00:01:00Z",
                    "updated_at": "2026-08-22T00:01:00Z",
                }
                for index in range(1, 102)
            ],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    first = provider.invoke(
        "listDesktopTasksV2",
        {"project_id": "project-1", "limit": 100, "after": None},
    )
    second = provider.invoke(
        "listDesktopTasksV2",
        {"project_id": "project-1", "limit": 100, "after": first.next_cursor},
    )

    assert len(first.items) == 100
    assert first.items[0].task_id == "session-001"
    assert first.items[-1].task_id == "session-100"
    assert first.next_cursor == "session-100"
    assert first.has_more is True
    assert [task.task_id for task in second.items] == ["session-101"]
    assert second.next_cursor is None
    assert second.has_more is False
    assert fake.task_observation_requests == 2


def test_provider_projects_daemon_project_heads_without_session_count_synthesis() -> None:
    fake = FakeDaemonClient()
    project_id = "project-durable-head"
    fake.state.update(
        {
            "active_project_id": project_id,
            "projects": [{
                "project_id": project_id,
                "display_name": "Durable Head project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "project_heads": [{
                "project_head_id": f"{project_id}-head-4",
                "project_id": project_id,
                "generation": 4,
                "predecessor_project_head_id": f"{project_id}-head-3",
                "source_evolution_run_id": "evolution-run-4",
                "artifact_ids": ["artifact-memory-4"],
                "workspace_manifest_sha256": "b" * 64,
                "workspace_entry_count": 3,
                "workspace_byte_size": 128,
                "manifest_sha256": "c" * 64,
                "created_at": "2026-08-22T00:04:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    project = provider.invoke("getDesktopProjectV2", {"project_id": project_id})
    head = provider.invoke(
        "getDesktopProjectHeadV2",
        {"project_head_id": f"{project_id}-head-4"},
    )

    assert project.active_project_head.project_head_id == f"{project_id}-head-4"
    assert project.active_project_head.generation == 4
    assert head.manifest_sha256 == "c" * 64
    assert head.workspace_snapshot.entry_count == 3
    assert head.evolution_revision.artifact_count == 1


def test_provider_submits_session_against_selected_historical_project_head() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [{
                "project_id": "project-1",
                "display_name": "Development project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "sessions": [{
                "session_id": "session-1",
                "project_id": "project-1",
                "project_head_id": "project-1-head-0",
                "task_title": "Historical task",
                "instruction": "Use the genesis context.",
                "state": "completed",
                "logs": ["completed"],
                "created_at": "2026-08-22T00:01:00Z",
                "updated_at": "2026-08-22T00:02:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)
    project = provider.invoke("getDesktopProjectV2", {"project_id": "project-1"})
    historical = provider.invoke(
        "getDesktopTaskV2", {"task_id": "session-1"}
    ).admission.predecessor_project_head

    task = provider.invoke(
        "submitDesktopTaskV2",
        {
            "request": core_m.TaskSubmitRequestV2(
                project_id=project.project_id,
                expected_project_admission_etag=project.admission_etag,
                expected_project_head_id=historical.project_head_id,
                expected_project_head_manifest_sha256=historical.manifest_sha256,
                expected_project_config_sha256=project.project_config_sha256,
            ),
            "idempotency_key": "historical-head-action-1",
            "resource_generation": historical.generation,
        },
    )

    assert task.admission.predecessor_project_head.project_head_id == "project-1-head-0"
    assert fake.state["sessions"][-1]["project_head_id"] == "project-1-head-0"


def test_provider_cancels_task_through_daemon_v2_authority() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [{
                "project_id": "project-1",
                "display_name": "Development project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "sessions": [{
                "session_id": "session-1",
                "project_id": "project-1",
                "task_title": "Long-running task",
                "instruction": "Keep working until cancelled.",
                "state": "running",
                "logs": ["started"],
                "created_at": "2026-08-22T00:01:00Z",
                "updated_at": "2026-08-22T00:01:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)
    task = provider.invoke("getDesktopTaskV2", {"task_id": "session-1"})

    operation = provider.invoke(
        "cancelDesktopTaskV2",
        {
            "task_id": task.task_id,
            "request": m.TaskActionV2(
                task_admission_id=task.admission.task_admission_id,
                admission_sha256=task.admission.admission_sha256,
                predecessor_project_head_id=(
                    task.admission.predecessor_project_head.project_head_id
                ),
            ),
            "idempotency_key": "cancel-action-00000001",
            "resource_generation": 1,
            "if_match": task.etag,
        },
    )

    assert operation.kind == "attempt_cancel"
    assert operation.status == "succeeded"
    assert operation.progress_completed == operation.progress_total == 1


def test_provider_reads_terminal_agent_result_from_daemon_v2_logs() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [{
                "project_id": "project-1",
                "display_name": "Development project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "sessions": [{
                "session_id": "session-1",
                "project_id": "project-1",
                "state": "completed",
                "logs": ["completed"],
                "response": "Authoritative v2 answer.",
                "error": None,
                "created_at": "2026-08-22T00:01:00Z",
                "updated_at": "2026-08-22T00:02:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)
    provider.invoke("listDesktopTasksV2", {"project_id": "project-1"})

    logs = provider.invoke(
        "getDesktopTaskLogsV2",
        {"task_id": "session-1", "limit": 100, "after": None},
    )

    assert logs.items[-1].stream == "transcript"
    assert logs.items[-1].message == "Authoritative v2 answer."


def test_provider_projects_daemon_v2_timeline_into_bound_desktop_events() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [{
                "project_id": "project-1",
                "display_name": "Development project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "sessions": [{
                "session_id": "session-1",
                "project_id": "project-1",
                "state": "running",
                "logs": ["started"],
                "created_at": "2026-08-22T00:01:00Z",
                "updated_at": "2026-08-22T00:01:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)
    task = provider.invoke("getDesktopTaskV2", {"task_id": "session-1"})

    first = provider.invoke(
        "getDesktopTaskTimelineV2",
        {"task_id": "session-1", "limit": 1, "after": None},
    )
    second = provider.invoke(
        "getDesktopTaskTimelineV2",
        {"task_id": "session-1", "limit": 100, "after": first.next_cursor},
    )

    assert first.has_more is True
    assert first.next_cursor == "1"
    assert first.items[0].event_type == "task_admitted"
    assert first.items[0].admission == task.admission
    assert second.items[0].event_type == "attempt_appended"
    assert second.items[0].attempt == task.attempts[0]


def test_project_catalog_keeps_inactive_projects_visible() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [
                {
                    "project_id": project_id,
                    "display_name": project_id,
                    "config": _config(),
                    "created_at": "2026-08-22T00:00:00Z",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
                for project_id in ("project-1", "project-2")
            ],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    projects = provider.invoke("listDesktopProjectsV2", {})

    assert [project.project_id for project in projects.items] == ["project-1", "project-2"]


def test_initial_snapshot_projections_share_the_bounded_state_cache() -> None:
    fake = FakeDaemonClient()
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    provider.invoke("getDesktopStateV2", {})
    provider.invoke("listRemoteWorkspaceProfilesV2", {})
    provider.invoke("listDesktopProjectsV2", {})
    provider.invoke("listDesktopTasksV2", {"project_id": None})

    assert fake.state_requests == 1


def test_state_and_profile_collection_publish_identical_authority() -> None:
    fake = FakeDaemonClient()
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    state = provider.invoke("getDesktopStateV2", {})
    profiles = provider.invoke("listRemoteWorkspaceProfilesV2", {})

    assert state.profiles == profiles.items
    assert state.profiles[0].etag == profiles.items[0].etag
    assert state.profiles[0].updated_at == profiles.items[0].updated_at


def test_development_api_proxy_requires_local_token_and_forwards_to_daemon() -> None:
    import openevo.web_gateway.product_app as web

    fake = FakeDaemonClient()
    calls: list[tuple[str, str, str, bytes, str | None]] = []

    def proxy(path: str, *, query: str, method: str, body: bytes, content_type: str | None):
        calls.append((path, query, method, body, content_type))
        return 200, b'{"schema_version":"1","status":"ok"}', {"Content-Type": "application/json"}

    fake.proxy = proxy  # type: ignore[attr-defined]
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8765",
            daemon_token="daemon-secret",
            session_token="web-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:5173",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    with TestClient(app) as client:
        unauthorized = client.get("/openevo-dev-agent/v1/state")
        assert unauthorized.status_code == 401
        response = client.post(
            "/openevo-dev-agent/v1/sessions?poll=true",
            headers={
                "X-OpenEvo-Development-Web-Token": "web-secret",
                "Content-Type": "application/json",
            },
            content=b'{"schema_version":"1"}',
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert calls == [
        ("sessions", "poll=true", "POST", b'{"schema_version":"1"}', "application/json")
    ]
