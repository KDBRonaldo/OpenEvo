"""Agent harness execution for the self-hosted OpenEvo daemon.

The process-local Session lifetime belongs to session_runtime. This module
owns the next layer: normalized Codex harness invocation and one admitted
Session's workspace/context/result transaction. The compatibility daemon
supplies its current runtime materializer and optional Evolution evidence
sealer while the remaining orchestration is migrated.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from openevo.backend.harness_adapter import (
    CodexHarnessAdapter,
    HarnessCancellation,
    HarnessRunCancelled,
    HarnessRunError,
)
from openevo.daemon.errors import AgentRunError
from openevo.daemon.workspace_store import ProjectWorkspaceStore


class AgentHarness(Protocol):
    def run(
        self,
        request: dict[str, Any],
        *,
        cancellation: HarnessCancellation | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]: ...


class AgentSessionStore(Protocol):
    def workspace_path(self, project_id: str) -> Path: ...

    def workspace_snapshot(self, project_id: str) -> dict[str, Any]: ...

    def latest_context_artifacts(self, project_id: str) -> list[dict[str, Any]]: ...

    def append_session_log(self, session_id: str, message: str) -> None: ...

    def apply_workspace_mutations(self, project_id: str, mutations: object) -> None: ...

    def complete_session(self, session_id: str, result: dict[str, Any]) -> None: ...

    def cancel_session(
        self,
        session_id: str,
        workspace_changes: list[dict[str, Any]],
    ) -> None: ...

    def fail_session(
        self,
        session_id: str,
        error: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None: ...


EvidenceSealer = Callable[[str, dict[str, str], dict[str, Any]], None]
AdapterFactory = Callable[..., Any]


class CodexAgentRunner:
    """Normalize the Codex harness adapter at the daemon boundary."""

    def __init__(
        self,
        *,
        codex_binary: str,
        timeout_seconds: int,
        model: str | None,
        context_materializer_factory: Callable[[], Any],
        runtime_control_adapter: Any,
        extract_event_logs: Callable[[str], list[str]],
        max_capture_bytes: int,
        max_response_bytes: int,
        max_workspace_context_bytes: int,
        adapter_factory: AdapterFactory = CodexHarnessAdapter,
    ) -> None:
        self._adapter = adapter_factory(
            codex_binary=codex_binary,
            timeout_seconds=timeout_seconds,
            model=model,
            context_materializer_factory=context_materializer_factory,
            runtime_control_adapter=runtime_control_adapter,
            extract_event_logs=extract_event_logs,
            max_capture_bytes=max_capture_bytes,
            max_response_bytes=max_response_bytes,
            max_workspace_context_bytes=max_workspace_context_bytes,
        )

    @property
    def codex_binary(self) -> str:
        return self._adapter.codex_binary

    @property
    def model(self) -> str | None:
        return self._adapter.model

    def runtime_capabilities(self) -> dict[str, Any]:
        return self._adapter.runtime_capabilities()

    def check_ready(self) -> None:
        try:
            self._adapter.check_ready()
        except HarnessRunError as exc:
            raise AgentRunError(str(exc)) from exc

    def run(
        self,
        request: dict[str, Any],
        *,
        cancellation: HarnessCancellation | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._adapter.run(request, cancellation=cancellation, log=log)
        except HarnessRunCancelled:
            raise
        except HarnessRunError as exc:
            raise AgentRunError(str(exc)) from exc


class AgentSessionExecutor:
    """Run one admitted Session through context, harness, and workspace commit."""

    def __init__(
        self,
        *,
        store: AgentSessionStore,
        runner: AgentHarness,
        evidence_sealer: EvidenceSealer | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._evidence_sealer = evidence_sealer

    def execute(
        self,
        session_id: str,
        request: dict[str, str],
        cancellation: HarnessCancellation,
    ) -> None:
        workspace_before: dict[str, Any] = {
            "project_id": request["project_id"],
            "entries": [],
            "truncated": False,
        }
        try:
            workspace_path = self._store.workspace_path(request["project_id"])
            workspace_before = self._store.workspace_snapshot(request["project_id"])
            execution_request = {
                **request,
                "workspace_path": workspace_path,
                "workspace_snapshot": workspace_before,
                "evolved_contexts": self._store.latest_context_artifacts(request["project_id"]),
            }
            result = self._runner.run(
                execution_request,
                cancellation=cancellation,
                log=lambda message: self._store.append_session_log(session_id, message),
            )
            cancellation.raise_if_requested()
            mutations = result.pop(
                "file_mutations",
                {"file_writes": [], "delete_paths": []},
            )
            self._store.apply_workspace_mutations(request["project_id"], mutations)
            workspace_after = self._store.workspace_snapshot(request["project_id"])
            result = {
                **result,
                "session_id": session_id,
                "workspace_changes": ProjectWorkspaceStore.changes(
                    workspace_before, workspace_after
                ),
                "workspace": workspace_after,
            }
            cancellation.raise_if_requested()
            self._store.complete_session(session_id, result)
            self._seal_evidence(session_id, request, result)
        except HarnessRunCancelled:
            workspace_after = self._store.workspace_snapshot(request["project_id"])
            self._store.cancel_session(
                session_id,
                ProjectWorkspaceStore.changes(workspace_before, workspace_after),
            )
        except (AgentRunError, HarnessRunError) as exc:
            workspace_after = self._store.workspace_snapshot(request["project_id"])
            self._store.fail_session(
                session_id,
                str(exc),
                ProjectWorkspaceStore.changes(workspace_before, workspace_after),
            )
        except Exception as exc:
            try:
                workspace_after = self._store.workspace_snapshot(request["project_id"])
                self._store.fail_session(
                    session_id,
                    f"unexpected development session failure: {exc}",
                    ProjectWorkspaceStore.changes(workspace_before, workspace_after),
                )
            except Exception:
                # Startup recovery remains authoritative if both execution and
                # final failure persistence are unavailable.
                pass

    def _seal_evidence(
        self,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
    ) -> None:
        if self._evidence_sealer is None:
            return
        try:
            self._evidence_sealer(session_id, request, result)
            self._store.append_session_log(
                session_id,
                "Session transcript sealed as reusable Evolution evidence.",
            )
        except Exception as exc:
            self._store.append_session_log(
                session_id,
                f"Session completed, but Evolution evidence sealing failed: {exc}",
            )
